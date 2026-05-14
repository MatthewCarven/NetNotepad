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
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Any

from netnotepad.log import log_exception


def _format_time(ts: float) -> str:
    """Format a timestamp for the per-peer header. Empty string when unknown."""
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


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

    text = tk.Text(
        root,
        undo=True,
        wrap="word",
        font=body_font,
        padx=10,
        pady=8,
        relief="flat",
        highlightthickness=0,
        height=12,
    )
    text.pack(side="top", fill="both", expand=True)
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

    peers_view = tk.Text(
        root,
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
    peers_view.pack(side="top", fill="both", expand=True)
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

    def refresh_peers_view() -> None:
        peers = engine.sorted_peers()
        peers_view.configure(state="normal")
        peers_view.delete("1.0", "end")
        if not peers:
            peers_view.insert("end", "  (no peers yet — waiting for someone to join)\n", "empty")
        else:
            for p in peers:
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
                body = (p.block_text or "(empty)") + "\n\n"
                head_tag = "tombstone_header" if p.tombstoned else "header"
                body_tag = ("tombstone_body",) if p.tombstoned else ()
                peers_view.insert("end", header_line, head_tag)
                peers_view.insert("end", body, body_tag)
        peers_view.configure(state="disabled")

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
    refresh_peers_view()
    root.mainloop()
