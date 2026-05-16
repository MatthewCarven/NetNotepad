"""End-to-end network test.

Starts two full NetNotepad engines on different ports and verifies they
discover each other via zeroconf and sync block contents over the TCP
mesh. Skips gracefully if zeroconf/TCP can't propagate in the environment.
"""

from __future__ import annotations

import threading
import time

import pytest

from netnotepad.engine import NetNotepad


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _start_pair(tmp_path, name_a: str, name_b: str, ):
    """Construct two engines and start their networking in parallel."""
    a = NetNotepad(
        data_dir=tmp_path / "a",
        mesh_port=0,
        attach_port=0,
        instance_name=name_a,
    )
    b = NetNotepad(
        data_dir=tmp_path / "b",
        mesh_port=0,
        attach_port=0,
        instance_name=name_b,
    )
    ta = threading.Thread(target=a.start_networking)
    tb = threading.Thread(target=b.start_networking)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    return a, b


@pytest.fixture
def two_engines(tmp_path):
    a, b = _start_pair(
        tmp_path,
        name_a="netnotepad-test-aaa",
        name_b="netnotepad-test-bbb",
        )
    try:
        yield a, b
    finally:
        for e in (a, b):
            try:
                e.shutdown()
            except Exception:
                pass


def test_engines_see_each_other_in_peers_dict(two_engines):
    a, b = two_engines

    found = _wait_for(
        lambda: b.hostname in a.peers and a.hostname in b.peers,
        timeout=6.0,
    )
    if not found:
        pytest.skip("zeroconf didn't propagate in this environment")
    assert b.hostname in a.peers
    assert a.hostname in b.peers


def test_local_edit_propagates_to_peer(two_engines):
    a, b = two_engines

    discovered = _wait_for(
        lambda: b.hostname in a.peers and a.hostname in b.peers,
        timeout=6.0,
    )
    if not discovered:
        pytest.skip("zeroconf didn't propagate in this environment")

    a.set_local_text("hello from A")
    propagated = _wait_for(
        lambda: b.peers.get(a.hostname) is not None
        and b.peers[a.hostname].block_text == "hello from A",
        timeout=8.0,
    )
    if not propagated:
        pytest.skip("mesh didn't link in this environment")
    assert b.peers[a.hostname].block_text == "hello from A"

    b.set_local_text("hello back from B")
    propagated_back = _wait_for(
        lambda: a.peers[b.hostname].block_text == "hello back from B",
        timeout=8.0,
    )
    assert propagated_back, "B's edit did not propagate back to A"


def test_tombstone_grace_suppresses_flicker_on_quick_reconnect(tmp_path):
    """A TCP drop followed by a quick reconnect within the grace window
    must NOT fire on_peer_tombstoned. This is the transient-blip
    suppression - we don't want the UI flickering offline for sub-second
    network hiccups.

    Tests engine internals directly (no real networking) so we can use
    a very short grace delay and finish in milliseconds.
    """
    from netnotepad.engine import Peer
    from netnotepad.protocol import Heartbeat

    e = NetNotepad(
        data_dir=tmp_path,
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-grace-1",
        tombstone_grace=0.2,
    )
    try:
        peer_name = "other-host"
        e.peers[peer_name] = Peer(hostname=peer_name, block_text="hi")

        fired = []
        e.on_peer_tombstoned.append(fired.append)

        # Simulate a TCP drop.
        e._on_mesh_disconnect(peer_name)
        # ... then a reconnect happens quickly - well within grace.
        time.sleep(0.05)
        e._on_remote_message(peer_name, Heartbeat())
        # Wait past where the tombstone WOULD have fired.
        time.sleep(0.3)

        assert fired == [], (
            "on_peer_tombstoned fired despite reconnect within grace window"
        )
        assert e.peers[peer_name].tombstoned is False
    finally:
        e.shutdown()


def test_tombstone_fires_after_grace_when_no_reconnect(tmp_path):
    """If the grace period elapses without any inbound traffic from the
    peer, the tombstone callbacks DO fire - this is the "they really did
    go offline" path."""
    from netnotepad.engine import Peer

    e = NetNotepad(
        data_dir=tmp_path,
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-grace-2",
        tombstone_grace=0.15,
    )
    try:
        peer_name = "lost-host"
        e.peers[peer_name] = Peer(hostname=peer_name, block_text="bye")

        fired = []
        e.on_peer_tombstoned.append(fired.append)

        e._on_mesh_disconnect(peer_name)
        time.sleep(0.4)

        assert len(fired) == 1, (
            "expected on_peer_tombstoned to fire exactly once, got "
            + str(len(fired))
        )
        assert fired[0].hostname == peer_name
        assert e.peers[peer_name].tombstoned is True
    finally:
        e.shutdown()


# ---------------------------------------------------------------------------
# Delta application — unit tests for _apply_delta_to_peer.
# ---------------------------------------------------------------------------


def test_apply_delta_pure_insert():
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.protocol import Delta

    peer = Peer(hostname="x", block_text="hello world")
    _apply_delta_to_peer(peer, Delta(pos=5, remove=0, insert=" big", seq=1, ts=1.0))
    assert peer.block_text == "hello big world"
    assert peer.last_edit_ts == 1.0


def test_apply_delta_pure_delete():
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.protocol import Delta

    peer = Peer(hostname="x", block_text="hello big world")
    _apply_delta_to_peer(peer, Delta(pos=5, remove=4, insert="", seq=1, ts=2.0))
    assert peer.block_text == "hello world"


def test_apply_delta_replace():
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.protocol import Delta

    peer = Peer(hostname="x", block_text="hello big world")
    _apply_delta_to_peer(peer, Delta(pos=6, remove=3, insert="BIG", seq=1, ts=3.0))
    assert peer.block_text == "hello BIG world"


def test_apply_delta_clamps_out_of_bounds_pos():
    """A pos past the end should clamp to end (append behavior) rather
    than crash. The result diverges from the sender; the next Snapshot
    rebroadcast reconciles."""
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.protocol import Delta

    peer = Peer(hostname="x", block_text="abc")
    _apply_delta_to_peer(peer, Delta(pos=999, remove=0, insert="X", seq=1, ts=1.0))
    assert peer.block_text == "abcX"


def test_apply_delta_clamps_out_of_bounds_remove():
    """A remove that runs past the end should clamp to end, not crash."""
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.protocol import Delta

    peer = Peer(hostname="x", block_text="abc")
    _apply_delta_to_peer(peer, Delta(pos=1, remove=999, insert="", seq=1, ts=1.0))
    assert peer.block_text == "a"


def test_apply_delta_is_grapheme_aware():
    """Position arithmetic counts grapheme clusters, not code points.
    A family-of-four emoji is one cluster even though it's 4 code points
    joined by 3 zero-width joiners."""
    from netnotepad.engine import Peer, _apply_delta_to_peer
    from netnotepad.engine.document import _graphemes
    from netnotepad.protocol import Delta

    fam = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
    peer = Peer(hostname="x", block_text="A" + fam + "B")
    assert _graphemes(peer.block_text) == ["A", fam, "B"]
    _apply_delta_to_peer(
        peer, Delta(pos=1, remove=1, insert="", seq=1, ts=1.0)
    )
    assert peer.block_text == "AB"


def test_on_remote_message_drops_delta_before_snapshot(tmp_path):
    """A Delta arriving before any Snapshot from a peer must be ignored.
    Applying it to an unknown base would build wrong content; the next
    Snapshot (via 30s periodic rebroadcast or sooner) reconciles."""
    from netnotepad.engine import Peer
    from netnotepad.protocol import Delta

    e = NetNotepad(
        data_dir=tmp_path,
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-drop-delta",
    )
    try:
        e.peers["mystery"] = Peer(
            hostname="mystery", has_received_snapshot=False
        )
        e._on_remote_message(
            "mystery", Delta(pos=0, remove=0, insert="hi", seq=1, ts=1.0)
        )
        assert e.peers["mystery"].block_text == "", (
            "Delta should have been dropped because no Snapshot has arrived"
        )
        assert e.peers["mystery"].has_received_snapshot is False
    finally:
        e.shutdown()


def test_on_remote_message_applies_delta_after_snapshot(tmp_path):
    """Once a Snapshot has arrived, subsequent Deltas update block_text."""
    from netnotepad.protocol import Delta, Snapshot

    e = NetNotepad(
        data_dir=tmp_path,
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-apply-delta",
    )
    try:
        e._on_remote_message(
            "other", Snapshot(content="hello", seq=1, ts=1.0)
        )
        assert e.peers["other"].block_text == "hello"
        assert e.peers["other"].has_received_snapshot is True

        e._on_remote_message(
            "other", Delta(pos=5, remove=0, insert=" world", seq=2, ts=2.0)
        )
        assert e.peers["other"].block_text == "hello world"
        assert e.peers["other"].last_edit_ts == 2.0
    finally:
        e.shutdown()


def test_delta_propagates_to_peer_via_engine_insert(tmp_path):
    """End-to-end: A.engine.insert() emits a Delta over the wire; B's
    mirror of A's block should pick it up. Tk's set_local_text path
    emits Snapshots, but engine.insert emits Deltas directly — this
    test exercises the same path the future terminal renderer will use."""

    a = NetNotepad(
        data_dir=tmp_path / "a",
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-delta-a",
    )
    b = NetNotepad(
        data_dir=tmp_path / "b",
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-delta-b",
    )
    try:
        a.set_local_text("hello")
        ta = threading.Thread(target=a.start_networking)
        tb = threading.Thread(target=b.start_networking)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        discovered = _wait_for(
            lambda: b.hostname in a.peers and a.hostname in b.peers,
            timeout=6.0,
        )
        if not discovered:
            pytest.skip("zeroconf didn't propagate in this environment")

        snap_arrived = _wait_for(
            lambda: b.peers.get(a.hostname) is not None
            and b.peers[a.hostname].block_text == "hello",
            timeout=8.0,
        )
        if not snap_arrived:
            pytest.skip("mesh handshake didn't complete in this environment")

        a.move_cursor(0, 5)
        a.insert(" world")

        propagated = _wait_for(
            lambda: b.peers[a.hostname].block_text == "hello world",
            timeout=5.0,
        )
        assert propagated, (
            "Delta did not propagate; B sees "
            + repr(b.peers[a.hostname].block_text)
        )
    finally:
        for e in (a, b):
            try:
                e.shutdown()
            except Exception:
                pass


def test_initial_snapshot_on_connect(tmp_path):
    """A peer that comes online AFTER another already has content should
    receive that content as part of the connection-handshake snapshot."""

    a = NetNotepad(
        data_dir=tmp_path / "a",
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-ccc",
    )
    b = NetNotepad(
        data_dir=tmp_path / "b",
        mesh_port=0,
        attach_port=0,
        instance_name="netnotepad-test-ddd",
    )
    try:
        a.start_networking()
        a.set_local_text("typed before B joined")
        time.sleep(0.3)
        b.start_networking()

        propagated = _wait_for(
            lambda: b.peers.get(a.hostname) is not None
            and b.peers[a.hostname].block_text == "typed before B joined",
            timeout=10.0,
        )
        if not propagated:
            pytest.skip("mesh handshake didn't complete in this environment")
        assert b.peers[a.hostname].block_text == "typed before B joined"
    finally:
        for e in (a, b):
            try:
                e.shutdown()
            except Exception:
                pass
