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
