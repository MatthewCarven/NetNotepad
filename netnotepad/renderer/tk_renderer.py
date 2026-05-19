"""
Tkinter renderer for netnotepad.

Layout (top to bottom):
    status bar         (hostname · peer count · transient state)
    our pane           (editable, mirrors engine.document)
    peers pane         (read-only, shows every remote peer's block)

Tk owns the editing UX (selection, paste, IME, undo). The widget's
contents mirror to ``engine.set_local_text`` on every <<Modified>> event;
that emits a Snapshot which the mesh broadcasts.

Threading: zeroconf and mesh callbacks fire on background threads. We
bounce them onto the Tk main loop via ``root.after(0, ...)`` which is
thread-safe.

The peers pane uses surgical per-peer updates rather than wipe-and-rebuild.
Each peer's section is anchored by a single left-gravity start mark, with
the end of the section computed as ``start_mark + content_len chars`` on
demand. A previous design used a second (right-gravity) end mark, but at
the boundary between consecutive peers the end mark would be dragged
forward by inserts at "end" and end up at the same buffer index as the
next peer's start mark, causing one peer's bracket to swallow another's
content. Tracking length instead of a second mark side-steps that. Combined
with a viewport-top anchor mark and a per-peer fingerprint cache, this
means: when one peer types, only that peer's section visibly redraws; the
viewport stays put; sections for other peers are not touched. The
full-rebuild path is reserved for structural changes (peer joins, leaves,
sort-order shifts).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont
from typing import Any

from netnotepad.engine.attachments import parse_attachment_tokens
from netnotepad.log import log_exception, log_info


def _format_time(ts: float) -> str:
    """Format a timestamp for the per-peer header. Empty string when unknown."""
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _peer_fingerprint(p: Any) -> tuple:
    """Capture every aspect of a peer that affects its visible rendering.

    Two peers with equal fingerprints produce byte-identical pane content,
    so the surgical refresh path can skip them entirely.
    """
    return (
        p.address,
        p.last_edit_ts,
        p.last_seen_ts,
        bool(p.tombstoned),
        bool(getattr(p, "has_received_snapshot", False)),
        p.block_text,
    )


def _format_peer_section(p: Any) -> tuple:
    """Build (header_line, body, head_tag, body_tag) for one peer."""
    header_bits = [p.hostname]
    if p.address:
        header_bits.append(p.address)
    edit_time = _format_time(p.last_edit_ts)
    if edit_time:
        header_bits.append(f"edited {edit_time}")
    elif p.tombstoned:
        seen = _format_time(p.last_seen_ts)
        if seen:
            header_bits.append(f"last seen {seen}")
    if p.tombstoned:
        header_bits.append("offline")
    header_line = "  " + "  ·  ".join(header_bits) + "\n"
    # Body ends with a single \n (the natural terminator). The visual blank
    # line between peers is added as a SEPARATOR insert OUTSIDE the section
    # brackets by the rebuild loop, so end_mark_<X> and start_mark_<X+1>
    # never end up at the same buffer position (which would let one peer's
    # bracket swallow another's content on surgical update).
    #
    # Three states for the body:
    #   - peer typed something          → show the text
    #   - peer typed an empty block     → "(empty)"
    #   - we haven't received a Snapshot yet → "(no content received yet)"
    # The third case is the one to surface clearly: zeroconf may have us
    # see a peer before any TCP handshake completed (or the handshake
    # might be flaky), in which case block_text is "" not because the
    # peer is genuinely empty but because we have no data for them.
    has_snap = bool(getattr(p, "has_received_snapshot", False))
    if has_snap:
        body = (p.block_text if p.block_text else "(empty)") + "\n"
    else:
        body = "(no content received yet)\n"
    head_tag = "tombstone_header" if p.tombstoned else "header"
    body_tag = ("tombstone_body",) if p.tombstoned else ()
    return header_line, body, head_tag, body_tag


def run(engine: Any) -> None:
    root = tk.Tk()
    root.title(f"netnotepad - {engine.hostname}")
    root.geometry("760x640")

    # ---------- top status bar ----------
    status_var = tk.StringVar(value=f"{engine.hostname}  ·  starting…")
    status = tk.Label(
        root,
        textvariable=status_var,
        anchor="w",
        font=tkfont.Font(family="Helvetica", size=10),
        fg="#666",
        padx=8,
        pady=4,
    )
    status.pack(side="top", fill="x")
    tk.Frame(root, height=1, bg="#ddd").pack(side="top", fill="x")

    state = {"transient": "ready"}

    def refresh_status() -> None:
        live = sum(1 for p in engine.peers.values() if not p.tombstoned)
        tomb = sum(1 for p in engine.peers.values() if p.tombstoned)
        parts = [engine.hostname]
        if live or tomb:
            peers_str = f"{live} peer(s)"
            if tomb:
                peers_str += f" ({tomb} offline)"
            parts.append(peers_str)
        parts.append(state["transient"])
        status_var.set("  ·  ".join(parts))

    def set_transient(s: str) -> None:
        state["transient"] = s
        refresh_status()

    body_font = tkfont.Font(family="Menlo", size=12)
    header_font = tkfont.Font(family="Helvetica", size=10, weight="bold")

    # ---------- our editable pane ----------
    own_header = tk.Label(
        root,
        text=f"  {engine.hostname}  (you)",
        anchor="w",
        font=header_font,
        fg="#333",
        bg="#f4f4f4",
        padx=4,
        pady=3,
    )
    own_header.pack(side="top", fill="x")

    # ---- attach toolbar: a single button that opens a file dialog ----
    def on_attach_clicked() -> None:
        path = filedialog.askopenfilename(parent=root, title="Attach file")
        if not path:
            return
        try:
            sha, token = engine.attach_file(path)
        except (OSError, ValueError) as e:
            log_exception(e, context="tk.attach_file")
            set_transient("attach failed: " + str(e))
            return
        # Insert the token at the current cursor. The <<Modified>> handler
        # will mirror the new text back into the engine so the Snapshot
        # broadcast carries it to peers, in addition to the AttachmentOffer
        # that engine.attach_file already kicked off.
        text.insert("insert", token)
        # Belt-and-braces retag here so the token is highlighted even if
        # the <<Modified>> chain races with this insert (programmatic
        # inserts sometimes don't fire <<Modified>> in the order you'd
        # expect, depending on Tk version).
        _retag_attachments(text, "1.0", text.get("1.0", "end-1c"))
        set_transient("attached " + Path(path).name)

    toolbar = tk.Frame(root, bg="#f4f4f4")
    toolbar.pack(side="top", fill="x")
    tk.Button(
        toolbar,
        text="Attach file...",
        relief="flat",
        command=on_attach_clicked,
    ).pack(side="left", padx=6, pady=2)

    own_container = tk.Frame(root)
    own_container.pack(side="top", fill="both", expand=True)
    text = tk.Text(
        own_container,
        undo=True,
        wrap="word",
        font=body_font,
        padx=10,
        pady=8,
        relief="flat",
        highlightthickness=0,
        height=12,
    )
    own_scroll = tk.Scrollbar(own_container, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=own_scroll.set)
    own_scroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    text.focus_set()

    initial = engine.document.text
    if initial:
        text.insert("1.0", initial)
        text.mark_set("insert", "end-1c")
    text.edit_modified(False)

    # ---------- read-only peers pane ----------
    tk.Frame(root, height=1, bg="#ddd").pack(side="top", fill="x")
    peers_label = tk.Label(
        root,
        text="  peers",
        anchor="w",
        font=header_font,
        fg="#666",
        bg="#f4f4f4",
        padx=4,
        pady=3,
    )
    peers_label.pack(side="top", fill="x")

    peers_container = tk.Frame(root)
    peers_container.pack(side="top", fill="both", expand=True)
    peers_view = tk.Text(
        peers_container,
        wrap="word",
        font=body_font,
        padx=10,
        pady=8,
        relief="flat",
        highlightthickness=0,
        height=14,
        state="disabled",
        bg="#fafafa",
    )
    peers_scroll = tk.Scrollbar(
        peers_container, orient="vertical", command=peers_view.yview
    )
    peers_view.configure(yscrollcommand=peers_scroll.set)
    peers_scroll.pack(side="right", fill="y")
    peers_view.pack(side="left", fill="both", expand=True)
    peers_view.tag_configure(
        "header", font=header_font, foreground="#666", spacing1=6, spacing3=2
    )
    peers_view.tag_configure(
        "tombstone_header",
        font=header_font,
        foreground="#aaa",
        spacing1=6,
        spacing3=2,
    )
    peers_view.tag_configure("tombstone_body", foreground="#999")
    peers_view.tag_configure("empty", foreground="#aaa", font=("Helvetica", 10, "italic"))
    # Attachment-token style: dark green text on a pale-yellow background +
    # bold + underline. Three independent signals on purpose - on a busy
    # screen, a single thin green underline at 12pt is too easy to miss.
    attachment_font = tkfont.Font(
        family=body_font.cget("family"),
        size=body_font.cget("size"),
        weight="bold",
    )
    for w in (peers_view, text):
        w.tag_configure(
            "attachment",
            foreground="#0a5",
            background="#fff6c2",
            underline=True,
            font=attachment_font,
        )

    def _retag_attachments(widget: tk.Text, start: str, body: str) -> None:
        """Apply the ``attachment`` tag to every token in ``body``.

        ``start`` is a Tk index string identifying the start of ``body``
        in ``widget``. Tokens are located via parse_attachment_tokens so
        the regex stays in one place.

        We resolve ``start`` to canonical ``"L.C"`` form via
        ``widget.index`` BEFORE building any offset expressions. Tk's
        index parser handles ``markname + Nc`` just fine in most places,
        but for hostnames containing ``-`` (e.g. ``DESKTOP-NM6GRPH``)
        the mark name itself contains a hyphen and the resulting
        ``__peer_start_DESKTOP-NM6GRPH + 42c`` expression was producing
        empty tag ranges in ``tag_add`` while ``index``/``insert``
        accepted the same expression. Resolving up front avoids the
        ambiguity entirely.
        """
        try:
            base = widget.index(start)
            widget.tag_remove("attachment", base, f"{base} + {len(body)}c")
            count = 0
            for _sha, _name, s, e in parse_attachment_tokens(body):
                widget.tag_add(
                    "attachment", f"{base} + {s}c", f"{base} + {e}c"
                )
                count += 1
            if count:
                # Quick telemetry so we can confirm from the log that the
                # tag is being applied even when it visually isn't (e.g.
                # if a future bug re-introduces priority/order issues).
                log_info(
                    "retagged " + str(count) + " token(s)",
                    context="tk.retag_attachments",
                )
        except tk.TclError as ex:
            log_exception(ex, context="tk.retag_attachments")

    # ---------- right-click context menu: Save attachment as... ----------
    def _token_at_click(widget: tk.Text, event: Any) -> tuple[str, str] | None:
        """Return (sha, filename) for the token under the click, or None.

        Looks at the entire current line; tokens never span a newline so a
        line-wide scan is sufficient and avoids any need to track tag
        ranges. The cursor's x-position is mapped to a char offset within
        the line so we can pick the matching token.
        """
        try:
            idx = widget.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        line_start = widget.index(f"{idx} linestart")
        line_end = widget.index(f"{idx} lineend")
        line_text = widget.get(line_start, line_end)
        col = int(idx.split(".")[1])
        for sha, name, s, e in parse_attachment_tokens(line_text):
            if s <= col < e:
                return sha, name
        return None

    def _peer_hostname_at_click(event: Any) -> str | None:
        """Walk the peer section start-marks to find which peer's section
        contains the clicked line. Returns None outside any section."""
        try:
            idx = peers_view.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        click_line = int(float(idx))
        best: tuple[str, int] | None = None
        for hostname, (start_mark, _) in peer_section_marks.items():
            try:
                start_line = int(float(peers_view.index(start_mark)))
            except tk.TclError:
                continue
            if start_line <= click_line and (best is None or start_line > best[1]):
                best = (hostname, start_line)
        return best[0] if best else None

    def _save_attachment_dialog(sha: str, filename: str, peer_hostname: str | None) -> None:
        """Prompt the user for a destination, fetching the blob if needed."""
        if not engine.attachment_store.has(sha):
            if peer_hostname is None:
                set_transient("attachment not cached and no source peer")
                return
            set_transient("fetching " + filename + "...")
            ok = engine.ensure_attachment(peer_hostname, sha)
            if not ok:
                set_transient("fetch failed for " + filename)
                return
        dest = filedialog.asksaveasfilename(
            parent=root, title="Save attachment as...", initialfile=filename
        )
        if not dest:
            return
        try:
            engine.attachment_store.save_to(sha, Path(dest))
        except OSError as e:
            log_exception(e, context="tk.save_attachment")
            set_transient("save failed: " + str(e))
            return
        set_transient("saved " + filename)

    def _show_attachment_menu(widget: tk.Text, event: Any, peer_hostname: str | None) -> None:
        hit = _token_at_click(widget, event)
        if hit is None:
            return
        sha, filename = hit
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(
            label="Save attachment as...",
            command=lambda: _save_attachment_dialog(sha, filename, peer_hostname),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # Right-click bindings. Button-3 is the canonical right-click on
    # Windows and X11; Button-2 covers older macOS three-button mice.
    # macOS two-button trackpads also fire Button-2, so binding both
    # gives us cross-platform coverage without a per-OS branch.
    for btn in ("<Button-3>", "<Button-2>"):
        text.bind(
            btn,
            lambda e: _show_attachment_menu(text, e, peer_hostname=None),
        )
        peers_view.bind(
            btn,
            lambda e: _show_attachment_menu(
                peers_view, e, peer_hostname=_peer_hostname_at_click(e)
            ),
        )

    # ---------- per-peer surgical-update state ----------
    # hostname -> (start_mark_name, content_len_in_chars). The start mark has
    # left gravity and stays anchored at the start of the peer's section even
    # under inserts at its position. The end of the section is computed on
    # demand as "start_mark + content_len chars", which side-steps the mark
    # drift that bit the earlier (end_mark, right-gravity) design: when we
    # appended a boundary separator at "end", a right-gravity end_mark sitting
    # at "end-1c" was dragged forward past the separator, ending up at the
    # same buffer index as the NEXT peer's start_mark. Coinciding mark indices
    # then caused one peer's bracket to swallow the other's content on
    # surgical update. Tracking length instead of a second mark avoids the
    # whole gravity dance.
    peer_section_marks: dict[str, tuple[str, int]] = {}
    # hostname -> last-rendered fingerprint; the surgical refresh skips any
    # peer whose current fingerprint matches the cached one.
    peer_fingerprints: dict[str, tuple] = {}

    def _full_rebuild_peers_view(peers: list) -> None:
        """Wipe and rebuild the entire peers pane.

        Used on initial paint and whenever the peer set or sort order changes
        (peer joins, leaves, gets renamed). For pure content updates,
        ``refresh_peers_view`` takes the surgical path instead.

        Captures the user's viewport anchor (which peer's section was at the
        top of the viewport, plus the line offset within it) BEFORE the wipe,
        and restores it afterwards so a peer join/leave doesn't slam the
        user's reading position back to the top.
        """
        anchor_hostname = None
        anchor_line_offset = 0
        try:
            top_index = peers_view.index("@0,0")
            top_line = int(float(top_index))
            best_start_line = -1
            for hostname, (start_mark, _content_len) in peer_section_marks.items():
                try:
                    start_line = int(float(peers_view.index(start_mark)))
                except tk.TclError:
                    continue
                if start_line <= top_line and start_line > best_start_line:
                    anchor_hostname = hostname
                    best_start_line = start_line
            if anchor_hostname is not None:
                anchor_line_offset = top_line - best_start_line
        except tk.TclError:
            pass

        for _hostname, (start_mark, _content_len) in list(peer_section_marks.items()):
            try:
                peers_view.mark_unset(start_mark)
            except tk.TclError:
                pass
        peer_section_marks.clear()
        peer_fingerprints.clear()

        peers_view.configure(state="normal")
        peers_view.delete("1.0", "end")
        if not peers:
            peers_view.insert(
                "end", "  (no peers yet — waiting for someone to join)\n", "empty"
            )
        else:
            for p in peers:
                start_mark = f"__peer_start_{p.hostname}"
                # Place the start mark FIRST with left gravity, so the content
                # we insert right after lands to its right and the mark stays
                # anchored to the start of this peer's section across
                # subsequent inserts at "end" or at the mark itself.
                peers_view.mark_set(start_mark, "end-1c")
                peers_view.mark_gravity(start_mark, "left")
                header_line, body, head_tag, body_tag = _format_peer_section(p)
                peers_view.insert("end", header_line, head_tag)
                peers_view.insert("end", body, body_tag)
                content_len = len(header_line) + len(body)
                peer_section_marks[p.hostname] = (start_mark, content_len)
                peer_fingerprints[p.hostname] = _peer_fingerprint(p)
                _retag_attachments(peers_view, start_mark, header_line + body)
                # Visual separator BETWEEN sections, OUTSIDE the section
                # length we cached above. Without this, the next peer's
                # start_mark would be placed at "end-1c" — which is also
                # exactly start_mark + content_len chars away from this
                # peer's start_mark, i.e., the same buffer index. Marks at
                # coinciding indices tangle on surgical update (see the
                # comment on peer_section_marks).
                peers_view.insert("end", "\n")
        peers_view.configure(state="disabled")

        try:
            if anchor_hostname and anchor_hostname in peer_section_marks:
                start_mark, _ = peer_section_marks[anchor_hostname]
                start_line = int(float(peers_view.index(start_mark)))
                target_line = start_line + anchor_line_offset
                peers_view.yview(f"{target_line}.0")
            else:
                peers_view.yview_moveto(0.0)
        except tk.TclError:
            pass

    def refresh_peers_view() -> None:
        """Bring the peers pane up to date.

        Surgical path (the common case): peer set + sort order unchanged from
        the cache. For each peer, compare the live fingerprint against the
        cached one. Only peers whose fingerprint changed get their section
        deleted and reinserted. A viewport-top anchor mark keeps the user's
        reading position stable when content above the viewport changes size.

        Full rebuild: peer set or sort order changed. Delegates to
        ``_full_rebuild_peers_view`` which also restores per-peer anchor.
        """
        peers = engine.sorted_peers()
        current_hostnames = [p.hostname for p in peers]
        cached_hostnames = list(peer_section_marks.keys())

        if current_hostnames != cached_hostnames:
            _full_rebuild_peers_view(peers)
            return

        if not current_hostnames:
            return

        try:
            peers_view.mark_set("__viewport_top", "@0,0")
            peers_view.mark_gravity("__viewport_top", "left")
        except tk.TclError:
            pass

        any_change = False
        surgical_failed = False
        peers_view.configure(state="normal")
        for p in peers:
            new_fp = _peer_fingerprint(p)
            if peer_fingerprints.get(p.hostname) == new_fp:
                continue
            any_change = True
            start_mark, old_content_len = peer_section_marks[p.hostname]
            try:
                # End of section is computed from start_mark + cached length,
                # so there is no second mark whose gravity could misbehave at
                # the boundary with the next peer's start_mark.
                end_idx = peers_view.index(
                    f"{start_mark} + {old_content_len}c"
                )
                peers_view.delete(start_mark, end_idx)
                header_line, body, head_tag, body_tag = _format_peer_section(p)
                # Insert header first; start_mark has left gravity so it
                # stays anchored at the very start of the section while the
                # new header lands to its right.
                peers_view.insert(start_mark, header_line, head_tag)
                # Now insert body immediately after the header. The index
                # "start_mark + len(header)c" resolves to the position right
                # after the freshly-inserted header, which is also where the
                # separator currently sits — so body goes between header and
                # separator, pushing the separator forward.
                peers_view.insert(
                    f"{start_mark} + {len(header_line)}c", body, body_tag
                )
            except tk.TclError as e:
                # Something went wrong mid-surgery (bad index expression,
                # missing mark, etc.) and the section may now be partially
                # rewritten. Don't leave a torn render on screen — flag a
                # full rebuild after the loop. Log so it isn't silent.
                log_exception(
                    e, context=f"tk.surgical_update[{p.hostname}]"
                )
                surgical_failed = True
                break
            new_content_len = len(header_line) + len(body)
            peer_section_marks[p.hostname] = (start_mark, new_content_len)
            peer_fingerprints[p.hostname] = new_fp
            _retag_attachments(peers_view, start_mark, header_line + body)
        peers_view.configure(state="disabled")

        if surgical_failed:
            # Surgical update threw — content is in an unknown state. Recover
            # with a full rebuild, which wipes everything and reinserts from
            # scratch so we're back to a known-good render.
            _full_rebuild_peers_view(peers)
            return

        if any_change:
            try:
                peers_view.yview("__viewport_top")
            except tk.TclError:
                pass

    # ---------- debounced save ----------
    pending_save: dict[str, Any] = {"job": None}

    def cancel_pending_save() -> None:
        if pending_save["job"] is not None:
            try:
                root.after_cancel(pending_save["job"])
            except tk.TclError:
                pass
            pending_save["job"] = None

    def schedule_save() -> None:
        cancel_pending_save()
        pending_save["job"] = root.after(500, do_save)

    def do_save() -> None:
        pending_save["job"] = None
        engine.save()
        set_transient("saved")

    # ---------- edit handler ----------
    def on_modified(event: Any = None) -> None:
        new_text = text.get("1.0", "end-1c")
        engine.set_local_text(new_text)
        text.edit_modified(False)
        _retag_attachments(text, "1.0", new_text)
        set_transient("editing…")
        schedule_save()

    text.bind("<<Modified>>", on_modified)

    # ---------- peer event subscribers ----------
    def on_peer_event(peer: Any = None) -> None:
        # Fires on zeroconf/mesh thread. Bounce to Tk main loop.
        root.after(0, refresh_status)
        root.after(0, refresh_peers_view)

    engine.on_peer_changed.append(on_peer_event)
    engine.on_peer_tombstoned.append(on_peer_event)

    # ---------- networking in background ----------
    def start_net() -> None:
        try:
            engine.start_networking()
        except Exception as e:
            log_exception(e, context="tk.start_networking")
            root.after(0, lambda: set_transient(f"network error: {e}"))
            return
        root.after(
            0,
            lambda: (
                root.title(f"netnotepad - {engine.hostname}"),
                own_header.configure(text=f"  {engine.hostname}  (you)"),
                set_transient("ready"),
            ),
        )

    threading.Thread(target=start_net, daemon=True).start()

    # ---------- shutdown ----------
    def on_close() -> None:
        cancel_pending_save()
        try:
            engine.save()
        except Exception as e:
            log_exception(e, context="tk.on_close.save")
        try:
            engine.shutdown()
        except Exception as e:
            log_exception(e, context="tk.on_close.shutdown")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_status()
    # Initial paint goes through the full-rebuild path so the empty-state
    # placeholder draws when there are no peers yet, and marks get created
    # for any peers already present. All subsequent updates flow through
    # refresh_peers_view via on_peer_event.
    _full_rebuild_peers_view(engine.sorted_peers())
    root.mainloop()
