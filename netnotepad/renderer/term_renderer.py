"""
Terminal renderer for netnotepad (prompt_toolkit).

This is the renderer that drives editing through the engine's **Delta** path —
``engine.insert`` / ``delete_backward`` / ``delete_forward`` / ``move_cursor`` —
rather than the Tk renderer's ``set_local_text`` (Snapshot) path. Every keystroke
emits an incremental ``Delta`` that the mesh broadcasts, so this is the renderer
that exercises the wire path the Tk UI never touches.

Layout (top to bottom, a full-screen ``HSplit``):

    status bar     (hostname · peer count · transient state)
    our header     (hostname (you))
    our block      (EDITABLE — a FormattedTextControl whose text is
                    engine.document.text and whose cursor is driven by
                    engine.document.cursor via get_cursor_position)
    separator
    peers header
    peers pane     (read-only; one section per peer, dimmed if tombstoned)
    dialog bar     (single line, only while a prompt is open)

The own block deliberately is NOT a prompt_toolkit ``Buffer`` / ``TextArea`` —
those own their own editing model, which would put us back in the Tk-style
"mirror the widget" posture that defeats the purpose. Instead the engine's
``Document`` is the single source of truth: a ``KeyBindings`` set translates each
key into an engine call and the control re-reads engine state on every render.
The dialog bar follows the same philosophy — its text lives in the shared ``ui``
dict, and ``Condition`` filters swap the key bindings between document mode and
dialog mode.

Threading: prompt_toolkit's event loop runs on the main thread, so key handlers
(and therefore all document edits and saves) are serialized there.
``engine.start_networking`` blocks ~1.5s and runs on a daemon thread. Peer
callbacks fire on zeroconf/mesh background threads and only call
``app.invalidate()`` (thread-safe) — the panes pull fresh engine state at render
time, so nothing is marshalled across the thread boundary. Fetching a peer's
attachment blob (Ctrl-D on an uncached sha) also runs on a daemon thread and
reports back via the transient status only.

Vertical movement remembers a goal (virtual) column: Up/Down through a short
line clamps the cursor but pops it back out on a long-enough line; any
horizontal move or edit forgets the goal. PageUp/PageDown scroll the peers pane
(the own block stays focused). The pane is pinned by a synthetic content cursor
at the scroll line because prompt_toolkit clamps a window's ``vertical_scroll``
to keep the content cursor visible — without it the pane snaps back to the top
on the next render.

Attachments: **Ctrl-A** prompts for a path and attaches the file (the returned
token is inserted at the cursor through the Delta path, so it propagates like
any other edit). **Ctrl-D** saves an attachment: the token under the cursor if
there is one, otherwise a numbered pick from every token visible in our block
and the peers' blocks; a second prompt takes the destination (a directory keeps
the original filename). Locally-cached blobs save synchronously; a peer's
uncached blob is fetched on a background thread via ``engine.ensure_attachment``.
Enter on an empty prompt, Escape, or Ctrl-G cancels.

File open / export (2026-06-12): **Ctrl-O** prompts for a path and REPLACES the
own block with the file's text (via ``engine.set_local_text`` — the Snapshot
path, mirroring Tk's File→Open; size/binary guards and encoding fallback live
in engine/fileio.py). **Ctrl-E** exports a block: with no peers it prompts
straight for a destination, otherwise a numbered pick (own block first, then
peers, departed peers included). A destination that is an existing directory
gets a ``<blockname>.txt`` default filename.

The own block wraps long lines (since 2026-06-10; Up/Down move by logical
line, not visual row). Known v1 limitation: on lines containing wide/zero-width
characters the cursor column (graphemes) can sit a cell off the rendered glyph.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from netnotepad.engine.attachments import parse_attachment_tokens
from netnotepad.engine.document import _graphemes
from netnotepad.engine.fileio import (
    block_choices,
    export_text,
    read_text_for_open,
    safe_filename,
)
from netnotepad.log import log_exception

# Idle delay before an edit triggers an autosave. Saves run on the event-loop
# (main) thread via call_later, so they never race the document mutation that
# happens in key handlers on the same thread.
AUTOSAVE_IDLE_SECONDS = 2.0

# Tab inserts spaces rather than a literal "\t" — terminal tab-width rendering
# is inconsistent and a scratchpad rarely wants real tab stops. (Decision noted
# in terminal_renderer_PLAN.md; flip to "\t" here if Matthew prefers.)
TAB_INSERT = "    "


# ----------------------------------------------------------------------------
# Pure helpers (no prompt_toolkit, no engine) — unit-testable in isolation.
# ----------------------------------------------------------------------------

def _line_lengths(text: str) -> list[int]:
    """Grapheme length of each logical line of ``text`` (split on ``\\n``)."""
    return [len(_graphemes(line)) for line in text.split("\n")]


def _move_left(text: str, line: int, col: int) -> tuple[int, int]:
    """Target (line, col) for a Left press. Wraps to end of previous line."""
    if col > 0:
        return line, col - 1
    if line > 0:
        lengths = _line_lengths(text)
        return line - 1, lengths[line - 1]
    return 0, 0


def _move_right(text: str, line: int, col: int) -> tuple[int, int]:
    """Target (line, col) for a Right press. Wraps to start of next line."""
    lengths = _line_lengths(text)
    cur_len = lengths[line] if 0 <= line < len(lengths) else 0
    if col < cur_len:
        return line, col + 1
    if line < len(lengths) - 1:
        return line + 1, 0
    return line, col


def _move_up(text: str, line: int, col: int, goal: int | None = None) -> tuple[int, int]:
    """Target for Up. ``goal`` is the remembered (virtual) column for a run of
    vertical moves: the cursor lands on ``min(goal, line length)``, so passing
    through a short line clamps it but a long-enough line pops it back out to
    the original column. ``goal=None`` falls back to the current column."""
    lengths = _line_lengths(text)
    want = col if goal is None else goal
    target = max(line - 1, 0)
    return target, min(want, lengths[target])


def _move_down(text: str, line: int, col: int, goal: int | None = None) -> tuple[int, int]:
    """Target for Down — same remembered-column rule as :func:`_move_up`."""
    lengths = _line_lengths(text)
    want = col if goal is None else goal
    target = min(line + 1, len(lengths) - 1)
    return target, min(want, lengths[target])


def _move_home(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for Home — start of the current line."""
    return line, 0


def _move_end(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for End — end of the current line."""
    lengths = _line_lengths(text)
    return line, lengths[line] if 0 <= line < len(lengths) else 0


def _page_scroll(current: int, page: int, content: int, pages: int) -> int:
    """New top line for the peers pane after moving ``pages`` pages of ``page``
    rows through ``content`` lines. Clamped to ``[0, content - page]``; 0 when
    the content already fits (including a stale ``current`` after shrink)."""
    if page <= 0 or content <= page:
        return 0
    return max(0, min(content - page, current + pages * page))


def _token_at_cursor(text: str, line: int, col: int) -> tuple[str, str] | None:
    """Return ``(sha, filename)`` for the attachment token under the cursor,
    or None. ``col`` is a grapheme column (the document's cursor unit); it is
    converted to a string index before comparing against token spans, so a
    token after an emoji on the same line is still found. The cursor counts as
    "on" a token from its first character through the position just after it
    (``s <= idx <= e``) — one keystroke after typing/attaching, Ctrl-D works.
    Tokens never span a newline, so a single-line scan is sufficient."""
    lines = text.split("\n")
    if not (0 <= line < len(lines)):
        return None
    g = _graphemes(lines[line])
    idx = sum(len(x) for x in g[:col])
    for sha, name, s, e in parse_attachment_tokens(lines[line]):
        if s <= idx <= e:
            return sha, name
    return None


def _visible_attachments(engine: Any) -> list[tuple[str, str, str | None]]:
    """Every attachment token currently on screen, de-duplicated by sha:
    ``(sha, filename, owner_hostname)`` — owner None for tokens in our own
    block, the peer's hostname for tokens in a peer's block (that's who
    ``ensure_attachment`` will fetch from). Ours first, then peers in
    ``sorted_peers()`` order."""
    out: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for sha, name, _s, _e in parse_attachment_tokens(engine.document.text):
        if sha not in seen:
            out.append((sha, name, None))
            seen.add(sha)
    for p in engine.sorted_peers():
        for sha, name, _s, _e in parse_attachment_tokens(p.block_text or ""):
            if sha not in seen:
                out.append((sha, name, p.hostname))
                seen.add(sha)
    return out


def _resolve_dest(dest: str, filename: str) -> Path:
    """Destination path for a save: ``~`` expanded; an existing directory
    keeps the attachment's original filename."""
    p = Path(dest).expanduser()
    if p.is_dir():
        return p / filename
    return p


def _format_time(ts: float) -> str:
    """HH:MM for a peer header; empty string when unknown."""
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _status_text(engine: Any, transient: str) -> str:
    """The status-bar string: hostname · peer count (offline) · transient."""
    peers = engine.sorted_peers()
    live = sum(1 for p in peers if not p.tombstoned)
    tomb = sum(1 for p in peers if p.tombstoned)
    parts = [engine.hostname]
    if live or tomb:
        peers_str = f"{live} peer(s)"
        if tomb:
            peers_str += f" ({tomb} offline)"
        parts.append(peers_str)
    parts.append(transient)
    return "  ·  ".join(parts)


def _format_peer_section(p: Any) -> tuple[str, str, bool]:
    """Return ``(header_line, body, tombstoned)`` for one peer.

    Mirrors the Tk renderer's body-state logic exactly: a peer that has sent a
    Snapshot shows its text (or ``(empty)`` for a deliberately empty block);
    one we have not heard content from yet shows ``(no content received yet)``
    so a stalled TCP handshake doesn't masquerade as an empty note.
    """
    bits = [p.hostname]
    if p.address:
        bits.append(p.address)
    edited = _format_time(p.last_edit_ts)
    if edited:
        bits.append("edited " + edited)
    elif p.tombstoned:
        seen = _format_time(p.last_seen_ts)
        if seen:
            bits.append("last seen " + seen)
    if p.tombstoned:
        bits.append("offline")
    header = "  " + "  ·  ".join(bits) + "\n"
    has_snap = bool(getattr(p, "has_received_snapshot", False))
    if has_snap:
        body = p.block_text if p.block_text else "(empty)"
    else:
        body = "(no content received yet)"
    if not body.endswith("\n"):
        body += "\n"
    return header, body, bool(p.tombstoned)


def _style_fragments(text: str, base_class: str) -> list[tuple[str, str]]:
    """Split ``text`` into (style, text) fragments, highlighting attachment
    tokens with the ``attachment`` style class and tagging everything else with
    ``base_class``. The concatenation of fragment texts equals ``text`` exactly,
    which keeps the own block's ``get_cursor_position`` column arithmetic valid.
    """
    spans = [(s, e) for _sha, _name, s, e in parse_attachment_tokens(text)]
    if not spans:
        return [(base_class, text)]
    frags: list[tuple[str, str]] = []
    i = 0
    for s, e in spans:
        if s > i:
            frags.append((base_class, text[i:s]))
        frags.append(("class:attachment", text[s:e]))
        i = e
    if i < len(text):
        frags.append((base_class, text[i:]))
    return frags


def _peers_fragments(engine: Any) -> list[tuple[str, str]]:
    """Formatted-text fragments for the whole read-only peers pane."""
    peers = engine.sorted_peers()
    if not peers:
        return [("class:empty", "  (no peers yet — waiting for someone to join)\n")]
    frags: list[tuple[str, str]] = []
    for p in peers:
        header, body, tomb = _format_peer_section(p)
        frags.append(("class:peer-header-off" if tomb else "class:peer-header", header))
        frags.extend(_style_fragments(body, "class:peer-body-off" if tomb else "class:peer-body"))
        frags.append(("", "\n"))  # blank line between peer sections
    return frags


_STYLE = Style.from_dict(
    {
        "status": "bg:#005f87 #ffffff",
        "own-header": "bg:#262626 #ffffff bold",
        "peers-header": "bg:#262626 #8a8a8a bold",
        "sep": "#444444",
        "peer-header": "#5fafff bold",
        "peer-header-off": "#6c6c6c bold",
        "peer-body": "",
        "peer-body-off": "#6c6c6c",
        "attachment": "#00af5f bold underline",
        "empty": "#8a8a8a italic",
        "dialog": "bg:#875f00 #ffffff bold",
    }
)


# ----------------------------------------------------------------------------
# Application construction
# ----------------------------------------------------------------------------

def _build(
    engine: Any,
    input: Any = None,
    output: Any = None,
    full_screen: bool = True,
) -> tuple[Application, dict]:
    """Construct the prompt_toolkit Application. Returns ``(app, ctl)`` where
    ``ctl`` exposes the handful of callables ``run`` needs (set_transient,
    do_save) plus the shared ``ui`` state dict. Does NOT start networking — so
    tests can build and drive the app headlessly without touching the LAN.
    """
    ui: dict[str, Any] = {
        "transient": "starting…",
        "dirty": False,
        "app": None,
        "autosave": None,
        "goal_col": None,    # remembered column for an Up/Down run; None = unset
        "peers_scroll": 0,   # top content line of the peers pane
        "dialog": None,      # {"prompt": str, "text": str, "accept": fn} | None
    }

    def invalidate() -> None:
        app = ui["app"]
        if app is None:
            return
        try:
            if app.is_running:
                app.invalidate()
        except Exception:
            # Never let a redraw request escape into a callback's thread.
            pass

    def set_transient(s: str) -> None:
        ui["transient"] = s
        invalidate()

    def do_save() -> None:
        ui["autosave"] = None
        try:
            engine.save()
            ui["dirty"] = False
            set_transient("saved")
        except Exception as e:
            log_exception(e, context="term.save")
            set_transient("save failed")

    def schedule_autosave() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. a unit test poking a handler) — skip
        if ui["autosave"] is not None:
            ui["autosave"].cancel()
        ui["autosave"] = loop.call_later(AUTOSAVE_IDLE_SECONDS, do_save)

    def mark_dirty() -> None:
        ui["dirty"] = True
        ui["goal_col"] = None  # an edit moves the cursor — forget the Up/Down goal
        set_transient("editing…")
        schedule_autosave()

    # ---- content callables (re-read engine state on every render) ----
    def own_text() -> list[tuple[str, str]]:
        return _style_fragments(engine.document.text, "")

    def own_cursor() -> Point:
        c = engine.document.cursor
        return Point(x=c.col, y=c.line)

    def status_text() -> list[tuple[str, str]]:
        return [("class:status", " " + _status_text(engine, ui["transient"]))]

    def own_header_text() -> list[tuple[str, str]]:
        return [("class:own-header", "  " + engine.hostname + "  (you)")]

    def peers_header_text() -> list[tuple[str, str]]:
        return [
            (
                "class:peers-header",
                "  peers   ·   ^A attach  ^D save-attachment  ^O open  ^E export  PgUp/PgDn scroll",
            )
        ]

    def peers_text() -> list[tuple[str, str]]:
        return _peers_fragments(engine)

    def peers_cursor() -> Point:
        # The pane is never focused, so this cursor is invisible. It exists to
        # pin the scroll position: prompt_toolkit clamps a window's
        # vertical_scroll so the content cursor stays visible, so a pane whose
        # cursor sat at (0, 0) would snap back to the top on every render.
        last = sum(t.count("\n") for _s, t in _peers_fragments(engine))
        ui["peers_scroll"] = max(0, min(ui["peers_scroll"], last))
        return Point(x=0, y=ui["peers_scroll"])

    # ---- dialog bar (attach / save-as prompts) ----
    dialog_active = Condition(lambda: ui["dialog"] is not None)

    def open_dialog(prompt: str, accept: Callable[[str], None]) -> None:
        ui["dialog"] = {"prompt": prompt, "text": "", "accept": accept}
        invalidate()

    def close_dialog() -> None:
        ui["dialog"] = None
        invalidate()

    def dialog_fragments() -> list[tuple[str, str]]:
        d = ui["dialog"]
        if d is None:
            return []
        return [("class:dialog", " " + d["prompt"] + d["text"])]

    own_window = Window(
        FormattedTextControl(text=own_text, focusable=True, get_cursor_position=own_cursor),
        # Wrapping ON (Matthew's call, 2026-06-10): long lines wrap like the Tk
        # UI. Up/Down still move by *logical* line — acceptable for a
        # scratchpad; revisit if it grates during real use.
        wrap_lines=True,
    )
    peers_window = Window(
        FormattedTextControl(text=peers_text, focusable=True, get_cursor_position=peers_cursor),
        wrap_lines=True,
    )
    dialog_bar = ConditionalContainer(
        Window(FormattedTextControl(text=dialog_fragments), height=1, style="class:dialog"),
        filter=dialog_active,
    )

    root = HSplit(
        [
            Window(FormattedTextControl(text=status_text), height=1, style="class:status"),
            Window(FormattedTextControl(text=own_header_text), height=1, style="class:own-header"),
            own_window,
            Window(height=1, char="─", style="class:sep"),
            Window(FormattedTextControl(text=peers_header_text), height=1, style="class:peers-header"),
            peers_window,
            dialog_bar,
        ]
    )

    # ---- attachment actions ----
    def _do_attach(path_str: str) -> None:
        path_str = path_str.strip()
        if not path_str:
            set_transient("attach cancelled")
            return
        p = Path(path_str).expanduser()
        try:
            _sha, token = engine.attach_file(p)
        except FileNotFoundError:
            set_transient("attach failed: no such file")
            return
        except IsADirectoryError:
            set_transient("attach failed: that's a directory")
            return
        except PermissionError:
            set_transient("attach failed: permission denied")
            return
        except ValueError as e:  # exceeds MAX_ATTACHMENT_BYTES
            log_exception(e, context="term.attach")
            set_transient("attach failed: file too large")
            return
        except OSError as e:
            log_exception(e, context="term.attach")
            set_transient("attach failed")
            return
        engine.insert(token)  # Delta path — propagates like any edit
        mark_dirty()
        set_transient("attached " + p.name)

    def _do_save_attachment(sha: str, filename: str, owner: str | None, dest_str: str) -> None:
        dest_str = dest_str.strip()
        if not dest_str:
            set_transient("save cancelled")
            return
        dest = _resolve_dest(dest_str, filename)

        def write_out() -> bool:
            try:
                engine.attachment_store.save_to(sha, dest)
                return True
            except OSError as e:
                log_exception(e, context="term.save_attachment")
                return False

        if engine.attachment_store.has(sha):
            # Cached (our own attachment, or an already-prefetched peer blob):
            # save synchronously — deterministic and instant.
            set_transient(("saved " + filename) if write_out() else "save failed")
            return

        def _bg() -> None:
            ok = False
            try:
                ok = owner is not None and engine.ensure_attachment(owner, sha)
            except Exception as e:
                log_exception(e, context="term.ensure_attachment")
            set_transient(("saved " + filename) if (ok and write_out()) else "save failed")

        set_transient("fetching " + filename + "…")
        threading.Thread(target=_bg, daemon=True).start()

    def _prompt_dest(sha: str, filename: str, owner: str | None) -> None:
        open_dialog(
            "save " + filename + " to: ",
            lambda s: _do_save_attachment(sha, filename, owner, s),
        )

    def _start_save_attachment() -> None:
        doc = engine.document
        hit = _token_at_cursor(doc.text, doc.cursor.line, doc.cursor.col)
        if hit is not None:
            _prompt_dest(hit[0], hit[1], None)
            return
        atts = _visible_attachments(engine)
        if not atts:
            set_transient("no attachments")
            return
        if len(atts) == 1:
            sha, name, owner = atts[0]
            _prompt_dest(sha, name, owner)
            return
        listing = "  ".join(
            str(i + 1) + ":" + name + ("" if owner is None else "(" + owner + ")")
            for i, (_sha, name, owner) in enumerate(atts)
        )

        def pick(s: str) -> None:
            s = s.strip()
            if not s:
                set_transient("save cancelled")
                return
            try:
                sha, name, owner = atts[int(s) - 1]
            except (ValueError, IndexError):
                set_transient("no such attachment")
                return
            _prompt_dest(sha, name, owner)

        open_dialog("save which?  " + listing + "  > ", pick)

    # ---- file open / export (the term equivalents of Tk's File menu) ----
    def _do_open(path_str: str) -> None:
        path_str = path_str.strip()
        if not path_str:
            set_transient("open cancelled")
            return
        try:
            content = read_text_for_open(Path(path_str))
        except FileNotFoundError:
            set_transient("open failed: no such file")
            return
        except IsADirectoryError:
            set_transient("open failed: that's a directory")
            return
        except PermissionError:
            set_transient("open failed: permission denied")
            return
        except ValueError as e:  # too large / binary — fileio's guards
            set_transient("open failed: " + str(e))
            return
        except OSError as e:
            log_exception(e, context="term.open")
            set_transient("open failed")
            return
        # Wholesale replace goes through the Snapshot path (set_local_text),
        # exactly like Tk's File->Open — peers get the new block in one
        # message instead of a giant Delta, and the cursor clamps to a valid
        # position in the new text.
        engine.set_local_text(content)
        mark_dirty()
        set_transient("opened " + Path(path_str).expanduser().name)

    def _do_export(label: str, content: str, dest_str: str) -> None:
        dest_str = dest_str.strip()
        if not dest_str:
            set_transient("export cancelled")
            return
        dest = _resolve_dest(dest_str, safe_filename(label) + ".txt")
        try:
            export_text(dest, content)
        except OSError as e:
            log_exception(e, context="term.export")
            set_transient("export failed")
            return
        set_transient("saved " + dest.name)

    def _start_export() -> None:
        choices = block_choices(
            engine.hostname + " (you)",
            engine.document.text,
            engine.sorted_peers(),
        )
        if len(choices) == 1:
            # Just us — skip the pick, go straight to the destination prompt.
            label, content = choices[0]
            open_dialog(
                "export " + label + " to: ",
                lambda s: _do_export(label, content, s),
            )
            return
        listing = "  ".join(
            str(i + 1) + ":" + label for i, (label, _c) in enumerate(choices)
        )

        def pick(s: str) -> None:
            s = s.strip()
            if not s:
                set_transient("export cancelled")
                return
            try:
                label, content = choices[int(s) - 1]
            except (ValueError, IndexError):
                set_transient("no such block")
                return
            open_dialog(
                "export " + label + " to: ",
                lambda t: _do_export(label, content, t),
            )

        open_dialog("export which?  " + listing + "  > ", pick)

    # ---- key bindings: every edit goes through the engine (Delta path) ----
    kb = KeyBindings()
    in_doc = ~dialog_active  # document-mode bindings are off while a prompt is open

    @kb.add(Keys.Any, filter=in_doc)
    def _(event: Any) -> None:
        data = event.data
        if data and data.isprintable():
            engine.insert(data)
            mark_dirty()

    @kb.add("enter", filter=in_doc)
    def _(event: Any) -> None:
        engine.insert("\n")
        mark_dirty()

    @kb.add("tab", filter=in_doc)
    def _(event: Any) -> None:
        engine.insert(TAB_INSERT)
        mark_dirty()

    @kb.add("backspace", filter=in_doc)
    def _(event: Any) -> None:
        engine.delete_backward()
        mark_dirty()

    @kb.add("delete", filter=in_doc)
    def _(event: Any) -> None:
        engine.delete_forward()
        mark_dirty()

    def _bind_move(
        key: str,
        fn: Callable[..., tuple[int, int]],
        vertical: bool = False,
    ) -> None:
        @kb.add(key, filter=in_doc)
        def _(event: Any) -> None:
            doc = engine.document
            if vertical:
                # Start a goal column on the first vertical move, keep it for
                # the rest of the run so short lines clamp without losing it.
                goal = ui["goal_col"] if ui["goal_col"] is not None else doc.cursor.col
                line, col = fn(doc.text, doc.cursor.line, doc.cursor.col, goal)
                ui["goal_col"] = goal
            else:
                ui["goal_col"] = None
                line, col = fn(doc.text, doc.cursor.line, doc.cursor.col)
            engine.move_cursor(line, col)
            invalidate()

    _bind_move("left", _move_left)
    _bind_move("right", _move_right)
    _bind_move("up", _move_up, vertical=True)
    _bind_move("down", _move_down, vertical=True)
    _bind_move("home", _move_home)
    _bind_move("end", _move_end)

    def _bind_page(key: str, pages: int) -> None:
        @kb.add(key, filter=in_doc)
        def _(event: Any) -> None:
            info = peers_window.render_info
            if info is not None:
                page = info.window_height
                content = info.ui_content.line_count
            else:
                # Not rendered yet — derive a workable page/content estimate.
                page = 10
                content = sum(t.count("\n") for _s, t in _peers_fragments(engine))
            ui["peers_scroll"] = _page_scroll(ui["peers_scroll"], page, content, pages)
            peers_window.vertical_scroll = ui["peers_scroll"]
            invalidate()

    _bind_page("pageup", -1)
    _bind_page("pagedown", 1)

    @kb.add("c-s", filter=in_doc)
    def _(event: Any) -> None:
        do_save()

    @kb.add("c-a", filter=in_doc)
    def _(event: Any) -> None:
        open_dialog("attach file: ", _do_attach)

    @kb.add("c-d", filter=in_doc)
    def _(event: Any) -> None:
        _start_save_attachment()

    @kb.add("c-o", filter=in_doc)
    def _(event: Any) -> None:
        open_dialog("open file: ", _do_open)

    @kb.add("c-e", filter=in_doc)
    def _(event: Any) -> None:
        _start_export()

    # ---- dialog-mode bindings ----
    @kb.add(Keys.Any, filter=dialog_active)
    def _(event: Any) -> None:
        data = event.data
        if data and data.isprintable():
            ui["dialog"]["text"] += data
            invalidate()

    @kb.add("backspace", filter=dialog_active)
    def _(event: Any) -> None:
        d = ui["dialog"]
        d["text"] = d["text"][:-1]
        invalidate()

    @kb.add("enter", filter=dialog_active)
    def _(event: Any) -> None:
        d = ui["dialog"]
        ui["dialog"] = None  # close before accept — accept may open the next prompt
        d["accept"](d["text"])
        invalidate()

    @kb.add("escape", filter=dialog_active)
    @kb.add("c-g", filter=dialog_active)
    def _(event: Any) -> None:
        close_dialog()
        set_transient("cancelled")

    # Quit stays global: it must work mid-dialog too.
    @kb.add("c-q")
    def _(event: Any) -> None:
        event.app.exit()

    @kb.add("c-c")
    def _(event: Any) -> None:
        event.app.exit()

    app = Application(
        layout=Layout(root, focused_element=own_window),
        key_bindings=kb,
        style=_STYLE,
        full_screen=full_screen,
        mouse_support=False,
        input=input,
        output=output,
    )
    ui["app"] = app
    # Exposed for tests (the layout tree offers no stable path to it).
    app._netnotepad_peers_window = peers_window  # type: ignore[attr-defined]

    # Peer events fire on background threads; just request a redraw. The panes
    # read fresh engine state when they re-render, so there's nothing to pass.
    def _on_peer_event(peer: Any = None) -> None:
        invalidate()

    engine.on_peer_changed.append(_on_peer_event)
    engine.on_peer_tombstoned.append(_on_peer_event)

    return app, {"set_transient": set_transient, "do_save": do_save, "ui": ui}


def build_application(
    engine: Any,
    *,
    input: Any = None,
    output: Any = None,
    full_screen: bool = True,
) -> Application:
    """Public builder used by tests. Returns the Application without starting
    networking. Pass a ``create_pipe_input()`` and ``DummyOutput()`` to drive it
    headlessly."""
    app, _ctl = _build(engine, input=input, output=output, full_screen=full_screen)
    return app


def run(engine: Any) -> None:
    """Entry point used by ``__main__`` for ``--renderer term``.

    Builds the full-screen app, starts networking on a daemon thread, runs the
    event loop, and saves + shuts the engine down on exit. (``__main__`` also
    calls ``engine.shutdown`` in its ``finally``; ``shutdown`` is idempotent.)
    """
    app, ctl = _build(engine)
    set_transient = ctl["set_transient"]

    def _start_net() -> None:
        try:
            engine.start_networking()
        except Exception as e:
            log_exception(e, context="term.start_networking")
            set_transient("network error")
            return
        set_transient("ready")

    threading.Thread(target=_start_net, daemon=True).start()

    try:
        app.run()
    finally:
        try:
            engine.save()
        except Exception as e:
            log_exception(e, context="term.save")
        try:
            engine.shutdown()
        except Exception as e:
            log_exception(e, context="term.shutdown")
