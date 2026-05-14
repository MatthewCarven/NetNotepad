"""Tests for the local Document model.

These exercise the cursor/insert/delete/persistence/set_text logic without
any networking. The engine and renderer layers are not covered here yet.
"""

from __future__ import annotations

from netnotepad.engine.document import Document


def test_insert_at_start():
    doc = Document()
    doc.insert("hello")
    assert doc.text == "hello"
    assert doc.cursor.line == 0
    assert doc.cursor.col == 5


def test_insert_then_delete_backward():
    doc = Document()
    doc.insert("hello")
    doc.delete_backward(2)
    assert doc.text == "hel"
    assert doc.cursor.col == 3


def test_newlines_track_lines():
    doc = Document()
    doc.insert("line1\nline2")
    assert doc.cursor.line == 1
    assert doc.cursor.col == 5


def test_cursor_move_clamps_to_valid():
    doc = Document()
    doc.insert("abc\ndef")
    doc.move_cursor(99, 99)
    assert doc.cursor.line == 1
    assert doc.cursor.col == 3


def test_move_cursor_negative_clamps_to_zero():
    doc = Document()
    doc.insert("abc")
    doc.move_cursor(-5, -5)
    assert doc.cursor.line == 0
    assert doc.cursor.col == 0


def test_emits_delta_on_insert():
    captured = []
    doc = Document(on_change=captured.append)
    doc.insert("hi")
    assert len(captured) == 1
    assert captured[0].insert == "hi"
    assert captured[0].pos == 0
    assert captured[0].remove == 0
    assert captured[0].seq == 1


def test_emits_delta_on_delete_backward():
    captured = []
    doc = Document(on_change=captured.append)
    doc.insert("abc")
    doc.delete_backward(1)
    assert len(captured) == 2
    assert captured[1].pos == 2
    assert captured[1].remove == 1
    assert captured[1].insert == ""


def test_delete_forward():
    doc = Document()
    doc.insert("hello")
    doc.move_cursor(0, 0)
    doc.delete_forward(2)
    assert doc.text == "llo"
    assert doc.cursor.col == 0


def test_delete_at_start_is_noop():
    doc = Document()
    doc.insert("abc")
    doc.move_cursor(0, 0)
    captured = []
    doc._on_change = captured.append
    doc.delete_backward(5)
    assert doc.text == "abc"
    assert captured == []


def test_insert_mid_block():
    doc = Document()
    doc.insert("ac")
    doc.move_cursor(0, 1)
    doc.insert("b")
    assert doc.text == "abc"
    assert doc.cursor.col == 2


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "mine.txt"
    doc = Document(save_path=path)
    doc.insert("persistent content")
    doc.save()

    doc2 = Document(save_path=path)
    assert doc2.text == "persistent content"
    assert doc2.cursor.line == 0
    assert doc2.cursor.col == len("persistent content")


def test_persistence_with_newlines(tmp_path):
    path = tmp_path / "mine.txt"
    doc = Document(save_path=path)
    doc.insert("line1\nline2\nline3")
    doc.save()

    doc2 = Document(save_path=path)
    assert doc2.text == "line1\nline2\nline3"
    assert doc2.cursor.line == 2
    assert doc2.cursor.col == 5


def test_set_text_replaces_block():
    doc = Document()
    doc.insert("hello world")
    doc.set_text("completely new content")
    assert doc.text == "completely new content"


def test_set_text_emits_snapshot():
    from netnotepad.protocol import Snapshot

    captured = []
    doc = Document(on_change=captured.append)
    doc.set_text("first")
    doc.set_text("second")
    assert len(captured) == 2
    assert all(isinstance(m, Snapshot) for m in captured)
    assert captured[0].content == "first"
    assert captured[1].content == "second"
    assert captured[1].seq > captured[0].seq


def test_set_text_noop_when_unchanged():
    captured = []
    doc = Document(on_change=captured.append)
    doc.set_text("same")
    doc.set_text("same")
    assert len(captured) == 1


def test_set_text_clamps_cursor():
    doc = Document()
    doc.insert("line1\nline2\nline3")
    doc.set_text("short")
    assert doc.cursor.line == 0
    assert doc.cursor.col == 5
