"""
Headless tests for the terminal renderer.

The sandbox/CI has no TTY, so we drive the prompt_toolkit Application through a
``create_pipe_input()`` + ``DummyOutput()`` pair: feed a scripted byte sequence
(ending in Ctrl-Q to exit), run the app, then assert on engine state. The
headline tests assert not just the resulting document text but the exact
sequence of ``Delta`` ops the engine emitted -- the wire path this renderer
exists to exercise (the Tk renderer only ever emits Snapshots).

Pure cursor/format helpers are tested directly with no Application at all.
"""

from __future__ import annotations

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from netnotepad.engine import NetNotepad, Peer
from netnotepad.protocol import Delta
from netnotepad.engine.attachments import make_attachment_token
from netnotepad.renderer import term_renderer as tr


# byte sequences a terminal sends for special keys
LEFT = "\x1b[D"
UP = "\x1b[A"
DOWN = "\x1b[B"
PGUP = "\x1b[5~"
PGDN = "\x1b[6~"
HOME = "\x1b[H"
DEL = "\x1b[3~"   # forward delete
BS = "\x7f"        # backspace
ENTER = "\r"
CSAVE = "\x13"     # Ctrl-S
CQUIT = "\x11"     # Ctrl-Q


def _engine(tmp_path, name="netnotepad-term-test"):
    return NetNotepad(data_dir=tmp_path, mesh_port=0, instance_name=name)


def _drive(engine, keys):
    """Feed `keys` to a headless build of the app and run until it exits.
    `keys` must end with CQUIT (or otherwise trigger app.exit())."""
    with create_pipe_input() as inp:
        app = tr.build_application(engine, input=inp, output=DummyOutput())
        inp.send_text(keys)
        app.run()


def _record(engine):
    msgs = []
    engine.on_local_change.append(msgs.append)
    return msgs


# ============================ pure helpers ============================

def test_line_lengths_counts_graphemes():
    assert tr._line_lengths("ab\ncde") == [2, 3]
    assert tr._line_lengths("") == [0]
    fam = "\U0001F468‍\U0001F469‍\U0001F467‍\U0001F466"
    assert tr._line_lengths(fam) == [1]


def test_move_left_within_line_and_wraps_up():
    assert tr._move_left("hello", 0, 3) == (0, 2)
    assert tr._move_left("ab\ncd", 1, 0) == (0, 2)
    assert tr._move_left("ab", 0, 0) == (0, 0)


def test_move_right_within_line_and_wraps_down():
    assert tr._move_right("hello", 0, 3) == (0, 4)
    assert tr._move_right("ab\ncd", 0, 2) == (1, 0)
    assert tr._move_right("ab", 0, 2) == (0, 2)


def test_move_up_down_clamp_column():
    assert tr._move_up("ab\nlongline", 1, 4) == (0, 2)
    assert tr._move_down("longline\nab", 0, 4) == (1, 2)
    assert tr._move_up("abc", 0, 2) == (0, 2)
    assert tr._move_down("abc", 0, 2) == (0, 2)


def test_move_home_end():
    assert tr._move_home("hello", 0, 3) == (0, 0)
    assert tr._move_end("hello", 0, 1) == (0, 5)


def test_status_text_no_peers(tmp_path):
    e = _engine(tmp_path, "boxA")
    assert tr._status_text(e, "ready") == "boxA  ·  ready"


def test_status_text_with_live_and_offline_peers(tmp_path):
    e = _engine(tmp_path, "boxA")
    e.peers["p1"] = Peer(hostname="p1")
    e.peers["p2"] = Peer(hostname="p2", tombstoned=True)
    s = tr._status_text(e, "ready")
    assert "boxA" in s and "1 peer(s)" in s and "(1 offline)" in s and "ready" in s


def test_format_peer_section_body_states():
    p = Peer(hostname="h", block_text="hi", has_received_snapshot=True)
    header, body, tomb = tr._format_peer_section(p)
    assert "h" in header and body.strip() == "hi" and tomb is False
    p2 = Peer(hostname="h", block_text="", has_received_snapshot=True)
    assert tr._format_peer_section(p2)[1].strip() == "(empty)"
    p3 = Peer(hostname="h", block_text="", has_received_snapshot=False)
    assert tr._format_peer_section(p3)[1].strip() == "(no content received yet)"
    p4 = Peer(hostname="h", block_text="x", has_received_snapshot=True, tombstoned=True)
    assert tr._format_peer_section(p4)[2] is True


def test_style_fragments_highlights_token_and_preserves_text():
    sha = "a" * 64
    token = make_attachment_token(sha, "pic.png")
    text = "see " + token + " ok"
    frags = tr._style_fragments(text, "")
    assert "".join(t for _s, t in frags) == text
    styled = [t for s, t in frags if s == "class:attachment"]
    assert styled == [token]


def test_style_fragments_plain_text_single_fragment():
    assert tr._style_fragments("plain", "class:peer-body") == [("class:peer-body", "plain")]


# ======================= headless integration =======================

def test_typing_emits_one_delta_per_char(tmp_path):
    e = _engine(tmp_path)
    msgs = _record(e)
    _drive(e, "hi" + CQUIT)
    assert e.document.text == "hi"
    deltas = [m for m in msgs if isinstance(m, Delta)]
    assert [d.insert for d in deltas] == ["h", "i"]
    assert all(d.remove == 0 for d in deltas)


def test_enter_inserts_newline(tmp_path):
    e = _engine(tmp_path)
    msgs = _record(e)
    _drive(e, "a" + ENTER + "b" + CQUIT)
    assert e.document.text == "a\nb"
    assert any(isinstance(m, Delta) and m.insert == "\n" for m in msgs)


def test_tab_inserts_spaces(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "\t" + "x" + CQUIT)
    assert e.document.text == tr.TAB_INSERT + "x"


def test_backspace_emits_delete_delta(tmp_path):
    e = _engine(tmp_path)
    msgs = _record(e)
    _drive(e, "abc" + BS + CQUIT)
    assert e.document.text == "ab"
    last = [m for m in msgs if isinstance(m, Delta)][-1]
    assert last.remove == 1 and last.insert == ""


def test_left_arrow_then_insert_positions_cursor(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "ac" + LEFT + "b" + CQUIT)
    assert e.document.text == "abc"


def test_home_then_forward_delete(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "abc" + HOME + DEL + CQUIT)
    assert e.document.text == "bc"


def test_ctrl_s_writes_mine_txt(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "hello" + CSAVE + CQUIT)
    assert (tmp_path / "mine.txt").read_text(encoding="utf-8") == "hello"


def test_build_application_registers_peer_callbacks(tmp_path):
    e = _engine(tmp_path)
    bc, bt = len(e.on_peer_changed), len(e.on_peer_tombstoned)
    with create_pipe_input() as inp:
        tr.build_application(e, input=inp, output=DummyOutput())
    assert len(e.on_peer_changed) == bc + 1
    assert len(e.on_peer_tombstoned) == bt + 1


# ================== remembered column / peers paging ==================

def test_move_up_down_remember_goal_column():
    text = "abcdef\nab\nabcdef"
    # with a goal, the clamp uses the goal rather than the current column
    assert tr._move_up(text, 1, 2, goal=6) == (0, 6)
    assert tr._move_down(text, 1, 2, goal=6) == (2, 6)
    # without a goal, behaviour is unchanged
    assert tr._move_up("ab\nlongline", 1, 4) == (0, 2)
    assert tr._move_down("longline\nab", 0, 4) == (1, 2)


def test_page_scroll_clamps():
    assert tr._page_scroll(0, 10, 100, 1) == 10
    assert tr._page_scroll(85, 10, 100, 1) == 90   # clamp to content - page
    assert tr._page_scroll(5, 10, 100, -1) == 0
    assert tr._page_scroll(0, 10, 8, 1) == 0       # content fits: never scroll
    assert tr._page_scroll(7, 10, 8, 1) == 0       # stale scroll after shrink resets


def test_up_through_short_line_keeps_column(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "abcdef" + ENTER + "ab" + ENTER + "abcdef" + UP + UP + "X" + CQUIT)
    # Up through the 2-char line clamps to col 2 but pops back out to col 6.
    assert e.document.text == "abcdefX\nab\nabcdef"


def test_horizontal_move_resets_goal_column(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "abcdef" + ENTER + "ab" + ENTER + "abcdef" + UP + LEFT + UP + "X" + CQUIT)
    # LEFT forgets the goal of 6; the second UP starts a fresh goal of 1.
    assert e.document.text == "aXbcdef\nab\nabcdef"


def test_edit_resets_goal_column(tmp_path):
    e = _engine(tmp_path)
    _drive(e, "abcdef" + ENTER + "ab" + ENTER + "abcdef" + UP + "Z" + UP + "X" + CQUIT)
    # Typing Z (at line 1 col 2) forgets the goal; second UP carries col 3.
    assert e.document.text == "abcXdef\nabZ\nabcdef"


def _long_peer_engine(tmp_path):
    e = _engine(tmp_path)
    e.peers["p1"] = Peer(
        hostname="p1", block_text="line\n" * 200, has_received_snapshot=True
    )
    return e


def test_pagedown_scrolls_peers_pane(tmp_path):
    e = _long_peer_engine(tmp_path)
    with create_pipe_input() as inp:
        app = tr.build_application(e, input=inp, output=DummyOutput())
        inp.send_text(PGDN + CQUIT)
        app.run()
        w = app._netnotepad_peers_window
    assert w.vertical_scroll > 0


def test_pageup_scrolls_peers_pane_back_to_top(tmp_path):
    e = _long_peer_engine(tmp_path)
    with create_pipe_input() as inp:
        app = tr.build_application(e, input=inp, output=DummyOutput())
        inp.send_text(PGDN + PGDN + PGUP + PGUP + CQUIT)
        app.run()
        w = app._netnotepad_peers_window
    assert w.vertical_scroll == 0
