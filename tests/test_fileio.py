"""Tests for netnotepad.engine.fileio - the display-free File-menu half."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from netnotepad.engine.fileio import (
    MAX_OPEN_BYTES,
    block_choices,
    export_text,
    read_text_for_open,
    safe_filename,
)


# ---------- read_text_for_open ----------

def test_open_plain_utf8(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\nworld", encoding="utf-8")
    assert read_text_for_open(f) == "hello\nworld"


def test_open_strips_bom_and_normalizes_crlf(tmp_path):
    f = tmp_path / "bom.txt"
    f.write_bytes("﻿line1\r\nline2\rline3".encode("utf-8"))
    assert read_text_for_open(f) == "line1\nline2\nline3"


def test_open_cp1252_fallback(tmp_path):
    f = tmp_path / "legacy.txt"
    f.write_bytes(b"caf\xe9")  # latin "é" - invalid as UTF-8
    assert read_text_for_open(f) == "café"


def test_open_rejects_binary(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"PK\x03\x04\x00\x00")
    with pytest.raises(ValueError, match="binary"):
        read_text_for_open(f)


def test_open_rejects_oversize(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"a" * (MAX_OPEN_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        read_text_for_open(f)


def test_open_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_text_for_open(tmp_path / "nope.txt")


def test_open_preserves_emoji_graphemes(tmp_path):
    f = tmp_path / "emoji.txt"
    text = "family: \U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
    f.write_text(text, encoding="utf-8")
    assert read_text_for_open(f) == text


# ---------- export_text ----------

def test_export_roundtrip(tmp_path):
    dest = tmp_path / "out.txt"
    export_text(dest, "some\ntext")
    assert dest.read_text(encoding="utf-8") == "some\ntext"


def test_export_creates_parent_dirs(tmp_path):
    dest = tmp_path / "deep" / "er" / "out.txt"
    export_text(dest, "x")
    assert dest.read_text(encoding="utf-8") == "x"


def test_export_overwrites_atomically(tmp_path):
    dest = tmp_path / "out.txt"
    dest.write_text("old", encoding="utf-8")
    export_text(dest, "new")
    assert dest.read_text(encoding="utf-8") == "new"
    assert not dest.with_suffix(".txt.tmp").exists()


# ---------- block_choices ----------

@dataclass
class _FakePeer:
    hostname: str
    block_text: str = ""
    has_received_snapshot: bool = True


def test_block_choices_own_first_then_peers():
    peers = [_FakePeer("alpha", "aa"), _FakePeer("beta", "bb")]
    assert block_choices("mine (HOST)", "me", peers) == [
        ("mine (HOST)", "me"),
        ("alpha", "aa"),
        ("beta", "bb"),
    ]


def test_block_choices_skips_peers_without_snapshot():
    peers = [
        _FakePeer("seen", "content"),
        _FakePeer("unseen", "", has_received_snapshot=False),
    ]
    labels = [label for label, _ in block_choices("mine", "", peers)]
    assert labels == ["mine", "seen"]


# ---------- safe_filename ----------

def test_safe_filename_passthrough():
    assert safe_filename("DESKTOP-NM6GRPH") == "DESKTOP-NM6GRPH"


def test_safe_filename_replaces_reserved_chars():
    assert safe_filename('box: "kitchen"/main') == "box_ _kitchen__main"


def test_safe_filename_never_empty():
    assert safe_filename("...") == "block"
    assert safe_filename("") == "block"
