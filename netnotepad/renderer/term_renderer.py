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

The own block deliberately is NOT a prompt_toolkit ``Buffer`` / ``TextArea`` —
those own their own editing model, which would put us back in the Tk-style
"mirror the widget" posture that defeats the purpose. Instead the engine's
``Document`` is the single source of truth: a ``KeyBindings`` set translates each
key into an engine call and the control re-reads engine state on every render.

Threading: prompt_toolkit's event loop runs on the main thread, so key handlers
(and therefore all document edits and saves) are serialized there.
``engine.start_networking`` blocks ~1.5s and runs on a daemon thread. Peer
callbacks fire on zeroconf/mesh background threads and only call
``app.invalidate()`` (thread-safe) — the panes pull fresh engine state at render
time, so nothing is marshalled across the thread boundary.

Known v1 limitations (see terminal_renderer_PLAN.md): the own block does not wrap
(long lines scroll horizontally) so the grapheme cursor maps cleanly to a screen
row; on lines containing wide/zero-width characters the cursor column (graphemes)
can sit a cell off the rendered glyph; vertical movement does not remember a
virtual column; the peers pane is not yet scrollable by key. Attachment tokens
render highlighted in both panes, but the attach / save-as UI is a follow-up.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from netnotepad.engine.attachments import parse_attachment_tokens
from netnotepad.engine.document import _graphemes
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


def _move_up(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for Up — same column, clamped to the line above's length."""
    if line <= 0:
        lengths = _line_lengths(text)
        return 0, min(col, lengths[0] if lengths else 0)
    lengths = _line_lengths(text)
    target = line - 1
    return target, min(col, lengths[target])


def _move_down(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for Down — same column, clamped to the line below's length."""
    lengths = _line_lengths(text)
    if line >= len(lengths) - 1:
        last = len(lengths) - 1
        return last, min(col, lengths[last]) if lengths else (0, 0)
    target = line + 1
    return target, min(col, lengths[target])


def _move_home(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for Home — start of the current line."""
    return line, 0


def _move_end(text: str, line: int, col: int) -> tuple[int, int]:
    """Target for End — end of the current line."""
    lengths = _line_lengths(text)
    return line, lengths[line] if 0 <= line < len(lengths) else 0


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
        return [("class:peers-header", "  peers")]

    def peers_text() -> list[tuple[str, str]]:
        return _peers_fragments(engine)

    own_window = Window(
        FormattedTextControl(text=own_text, focusable=True, get_cursor_position=own_cursor),
        wrap_lines=False,
    )
    peers_window = Window(
        FormattedTextControl(text=peers_text, focusable=True),
        wrap_lines=True,
    )

    root = HSplit(
        [
            Window(FormattedTextControl(text=status_text), height=1, style="class:status"),
            Window(FormattedTextControl(text=own_header_text), height=1, style="class:own-header"),
            own_window,
            Window(height=1, char="─", style="class:sep"),
            Window(FormattedTextControl(text=peers_header_text), height=1, style="class:peers-header"),
            peers_window,
        ]
    )

    # ---- key bindings: every edit goes through the engine (Delta path) ----
    kb = KeyBindings()

    @kb.add(Keys.Any)
    def _(event: Any) -> None:
        data = event.data
        if data and data.isprintable():
            engine.insert(data)
            mark_dirty()

    @kb.add("enter")
    def _(event: Any) -> None:
        engine.insert("\n")
        mark_dirty()

    @kb.add("tab")
    def _(event: Any) -> None:
        engine.insert(TAB_INSERT)
        mark_dirty()

    @kb.add("backspace")
    def _(event: Any) -> None:
        engine.delete_backward()
        mark_dirty()

    @kb.add("delete")
    def _(event: Any) -> None:
        engine.delete_forward()
        mark_dirty()

    def _bind_move(key: str, fn: Callable[[str, int, int], tuple[int, int]]) -> None:
        @kb.add(key)
        def _(event: Any) -> None:
            doc = engine.document
            line, col = fn(doc.text, doc.cursor.line, doc.cursor.col)
            engine.move_cursor(line, col)
            invalidate()

    _bind_move("left", _move_left)
    _bind_move("right", _move_right)
    _bind_move("up", _move_up)
    _bind_move("down", _move_down)
    _bind_move("home", _move_home)
    _bind_move("end", _move_end)

    @kb.add("c-s")
    def _(event: Any) -> None:
        do_save()

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
