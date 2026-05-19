"""Tests for the attachment store, HTTP server, fetch client, and engine wiring."""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from netnotepad.engine import NetNotepad, Peer
from netnotepad.engine.attachments import (
    AttachmentServer,
    AttachmentStore,
    MAX_ATTACHMENT_BYTES,
    fetch_blob,
    guess_mime,
    make_attachment_token,
    parse_attachment_tokens,
)
from netnotepad.protocol import AttachmentOffer, Hello


# --------------------------- pure helpers ---------------------------


def test_make_attachment_token_round_trips():
    sha = "a" * 64
    tok = make_attachment_token(sha, "hello.png")
    assert tok == "![attachment:" + ("a" * 64) + ":hello.png]"


def test_make_attachment_token_strips_dangerous_chars():
    sha = "b" * 64
    tok = make_attachment_token(sha, "evil]\nfile\t[here].txt")
    # Brackets, newlines, tabs scrubbed; the resulting token still parses.
    parsed = parse_attachment_tokens(tok)
    assert len(parsed) == 1
    found_sha, found_name, _, _ = parsed[0]
    assert found_sha == sha
    assert "\n" not in found_name and "\t" not in found_name and "[" not in found_name


def test_parse_attachment_tokens_multiple_in_text():
    sha1 = "1" * 64
    sha2 = "2" * 64
    body = (
        "before "
        + make_attachment_token(sha1, "a.png")
        + " middle "
        + make_attachment_token(sha2, "b.pdf")
        + " end"
    )
    tokens = parse_attachment_tokens(body)
    assert [t[0] for t in tokens] == [sha1, sha2]
    assert [t[1] for t in tokens] == ["a.png", "b.pdf"]


def test_parse_attachment_tokens_rejects_uppercase_sha():
    # Uppercase hex isn't a valid token; the regex demands lowercase to
    # prevent two cosmetically-different tokens collapsing to one blob.
    bad = "![attachment:" + ("A" * 64) + ":x.png]"
    assert parse_attachment_tokens(bad) == []


def test_guess_mime_known_and_unknown():
    assert guess_mime("foo.png") == "image/png"
    assert guess_mime("foo.unknownext") == "application/octet-stream"


# --------------------------- store ---------------------------


def test_store_round_trip(tmp_path):
    store = AttachmentStore(tmp_path)
    data = b"hello world"
    sha = store.put_bytes(data)
    assert sha == hashlib.sha256(data).hexdigest()
    assert store.has(sha)
    assert store.read(sha) == data
    assert store.size(sha) == len(data)
    assert store.path_for(sha).is_file()


def test_store_put_bytes_idempotent(tmp_path):
    store = AttachmentStore(tmp_path)
    data = b"abc"
    sha1 = store.put_bytes(data)
    mtime1 = store.path_for(sha1).stat().st_mtime_ns
    time.sleep(0.01)
    sha2 = store.put_bytes(data)
    mtime2 = store.path_for(sha2).stat().st_mtime_ns
    assert sha1 == sha2
    # Same sha + already-cached -> no rewrite, mtime should be unchanged.
    assert mtime1 == mtime2


def test_store_save_to_copies_blob(tmp_path):
    store = AttachmentStore(tmp_path / "store")
    sha = store.put_bytes(b"copy me")
    dest = tmp_path / "out.bin"
    store.save_to(sha, dest)
    assert dest.read_bytes() == b"copy me"


def test_store_save_to_missing_raises(tmp_path):
    store = AttachmentStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.save_to("0" * 64, tmp_path / "out.bin")


# --------------------------- server + fetch ---------------------------


@pytest.fixture
def running_server(tmp_path):
    store = AttachmentStore(tmp_path / "server_store")
    server = AttachmentServer(store, port=0)  # kernel-assigned
    server.start()
    try:
        yield store, server
    finally:
        server.stop()


def test_server_serves_cached_blob(running_server):
    store, server = running_server
    data = b"the quick brown fox"
    sha = store.put_bytes(data)
    url = "http://127.0.0.1:" + str(server.listen_port) + "/blob/" + sha
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = resp.read()
        assert resp.status == 200
    assert body == data


def test_server_404_on_unknown_sha(running_server):
    _store, server = running_server
    url = "http://127.0.0.1:" + str(server.listen_port) + "/blob/" + ("0" * 64)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url, timeout=5)
    assert exc.value.code == 404


def test_server_400_on_bad_sha(running_server):
    _store, server = running_server
    url = "http://127.0.0.1:" + str(server.listen_port) + "/blob/notahash"
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url, timeout=5)
    assert exc.value.code == 400


def test_server_404_on_bad_route(running_server):
    _store, server = running_server
    url = "http://127.0.0.1:" + str(server.listen_port) + "/wrong/path"
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url, timeout=5)
    assert exc.value.code == 404


def test_fetch_blob_downloads_and_verifies(tmp_path, running_server):
    server_store, server = running_server
    data = b"payload " * 100
    sha = server_store.put_bytes(data)

    client_store = AttachmentStore(tmp_path / "client_store")
    ok = fetch_blob("127.0.0.1", server.listen_port, sha, client_store)
    assert ok is True
    assert client_store.has(sha)
    assert client_store.read(sha) == data


def test_fetch_blob_short_circuits_on_cache_hit(tmp_path):
    """If the blob is already cached, fetch_blob returns True without
    touching the network. We pass a deliberately-invalid port to prove
    no socket was opened."""
    store = AttachmentStore(tmp_path)
    sha = store.put_bytes(b"already here")
    ok = fetch_blob("127.0.0.1", 1, sha, store, timeout=1.0)
    assert ok is True


def test_fetch_blob_sha_mismatch_returns_false(tmp_path, running_server):
    """If the server returns bytes whose sha doesn't match the requested
    one, fetch_blob refuses to cache them. We simulate by asking for a
    sha that doesn't exist while the server has a DIFFERENT blob - the
    request will 404, so fetch_blob returns False without poisoning the
    client cache."""
    _server_store, server = running_server
    client = AttachmentStore(tmp_path / "client")
    wrong_sha = hashlib.sha256(b"never stored").hexdigest()
    ok = fetch_blob("127.0.0.1", server.listen_port, wrong_sha, client)
    assert ok is False
    assert not client.has(wrong_sha)


def test_server_port_fallback_when_requested_port_in_use(tmp_path):
    """Two servers asking for the same explicit port: second one falls
    back to a kernel-assigned port rather than crashing the engine."""
    store_a = AttachmentStore(tmp_path / "a")
    store_b = AttachmentStore(tmp_path / "b")
    s_a = AttachmentServer(store_a, port=0)
    s_a.start()
    try:
        # Steal s_a's port and try to bind it again with s_b.
        s_b = AttachmentServer(store_b, port=s_a.listen_port)
        s_b.start()
        try:
            assert s_b.listen_port != s_a.listen_port
            assert s_b.listen_port != 0
        finally:
            s_b.stop()
    finally:
        s_a.stop()


# --------------------------- engine integration ---------------------------


@pytest.fixture
def engine(tmp_path):
    """An engine with no networking — we drive _on_remote_message directly."""
    e = NetNotepad(
        data_dir=tmp_path / "engine",
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-engine",
    )
    try:
        yield e
    finally:
        try:
            e.shutdown()
        except Exception:
            pass


def test_engine_attach_bytes_caches_and_returns_token(engine):
    sha, token = engine.attach_bytes(b"some bytes", filename="note.txt")
    assert engine.attachment_store.has(sha)
    assert token == "![attachment:" + sha + ":note.txt]"


def test_engine_attach_bytes_rejects_oversize(engine):
    with pytest.raises(ValueError):
        engine.attach_bytes(b"x" * (MAX_ATTACHMENT_BYTES + 1), filename="huge.bin")


def test_engine_attach_file_reads_from_disk(engine, tmp_path):
    src = tmp_path / "drop.txt"
    src.write_bytes(b"on disk")
    sha, token = engine.attach_file(src)
    assert engine.attachment_store.read(sha) == b"on disk"
    assert "drop.txt" in token


def test_engine_records_attachment_offer_from_remote(engine):
    """Without networking, drive _on_remote_message directly to verify
    the offer lands in peer.known_attachments."""
    offer = AttachmentOffer(
        sha256="c" * 64,
        filename="x.png",
        size=42,
        mime="image/png",
    )
    engine._on_remote_message("netnotepad-test-other", offer)
    peer = engine.peers["netnotepad-test-other"]
    assert "c" * 64 in peer.known_attachments
    assert peer.known_attachments["c" * 64].filename == "x.png"


def test_engine_captures_attach_port_from_hello(engine):
    hello = Hello(
        hostname="netnotepad-test-other",
        pid=999,
        attachment_port=54321,
    )
    engine._on_remote_message("netnotepad-test-other", hello)
    peer = engine.peers["netnotepad-test-other"]
    assert peer.attach_port == 54321


def test_engine_ensure_attachment_uses_known_peer(engine, tmp_path):
    """End-to-end: spin a second engine's-eye-view by running an
    AttachmentServer on its own store, then have ``engine`` fetch from
    it via ensure_attachment(). Validates that the engine looks up the
    peer's address+attach_port and pulls the blob."""
    serving_store = AttachmentStore(tmp_path / "serving")
    serving_server = AttachmentServer(serving_store, port=0)
    serving_server.start()
    try:
        sha = serving_store.put_bytes(b"hello from across the LAN")
        # Inject a peer record with known address+attach_port.
        with engine._peers_lock:
            engine.peers["remote"] = Peer(
                hostname="remote",
                address="127.0.0.1",
                attach_port=serving_server.listen_port,
            )
        ok = engine.ensure_attachment("remote", sha, timeout=5.0)
        assert ok is True
        assert engine.attachment_store.has(sha)
        assert engine.attachment_store.read(sha) == b"hello from across the LAN"
    finally:
        serving_server.stop()


def test_engine_ensure_attachment_missing_peer_returns_false(engine):
    ok = engine.ensure_attachment("never-existed", "0" * 64, timeout=1.0)
    assert ok is False


def test_engine_ensure_attachment_short_circuits_when_cached(engine):
    """If the blob is already cached locally, ensure_attachment returns
    True without needing peer state at all."""
    sha = engine.attachment_store.put_bytes(b"already mine")
    ok = engine.ensure_attachment("any-peer", sha, timeout=1.0)
    assert ok is True
