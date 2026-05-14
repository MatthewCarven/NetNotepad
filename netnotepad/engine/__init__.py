"""
NetNotepad engine - the headless core.

Renderers (Tkinter, terminal, tests) drive the engine through its
``NetNotepad`` facade and subscribe to its callback lists for events.

The engine owns:
  * the local Document (our own block + cursor + persistence)
  * the peer mirror state (``self.peers``)
  * discovery via zeroconf
  * the TCP mesh that syncs blocks
  * the attachment HTTP server + cache (TODO)
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from netnotepad.engine.discovery import (
    ATTACH_PORT,
    Discovery,
    DiscoveredPeer,
    MESH_PORT,
)
from netnotepad.engine.document import Cursor, Document
from netnotepad.engine.network import Mesh
from netnotepad.log import log_exception, log_info, set_log_dir
from netnotepad.protocol import Delta, Goodbye, Heartbeat, Hello, Snapshot


DEFAULT_DATA_DIR = Path.home() / ".netnotepad"

# How often we re-broadcast our snapshot defensively, even without an edit,
# so that any peer that missed a message gets re-synced.
PERIODIC_REBROADCAST_INTERVAL = 30.0

# How long to wait before retrying a dropped connection.
RECONNECT_DELAY = 2.0

# How long after a TCP drop before we actually show the peer as offline.
# The reconnect attempt fires at RECONNECT_DELAY; if it succeeds within
# TOMBSTONE_GRACE_DELAY the UI never flickers offline at all. Tuned to
# absorb brief WiFi blips, GC pauses on the peer, kernel scheduling
# stalls, and "antivirus just decided to scan a socket" moments without
# making real disconnects feel sluggish.
TOMBSTONE_GRACE_DELAY = 5.0


@dataclass
class Peer:
    """Mirror of another peer's state."""

    hostname: str
    address: str = ""
    last_seen_ts: float = 0.0
    last_edit_ts: float = 0.0
    block_text: str = ""
    tombstoned: bool = False


class NetNotepad:
    """The engine facade. Renderers should talk to this and nothing else."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        mesh_port: int = MESH_PORT,
        attach_port: int = ATTACH_PORT,
        instance_name: Optional[str] = None,
        tombstone_grace: float = TOMBSTONE_GRACE_DELAY,
    ):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.hostname = instance_name or socket.gethostname()
        self._mesh_port = mesh_port
        self._attach_port = attach_port
        self._tombstone_grace = tombstone_grace

        # Point the diagnostic logger at our data dir before anything else
        # so any subsequent failure in this constructor is captured too.
        set_log_dir(self.data_dir)

        self.document = Document(
            save_path=self.data_dir / "mine.txt",
            on_change=self._on_local_change,
        )

        self.peers: dict[str, Peer] = {}
        self._peers_lock = threading.RLock()
        self._discovery: Optional[Discovery] = None
        self._mesh: Optional[Mesh] = None

        # Last-known DiscoveredPeer per hostname - used to retry connections
        # after a TCP drop without waiting for the next zeroconf event.
        self._discovered: dict[str, DiscoveredPeer] = {}
        self._discovered_lock = threading.Lock()

        # Periodic snapshot rebroadcast thread.
        self._periodic_thread: Optional[threading.Thread] = None
        self._periodic_stop = threading.Event()

        # Pending tombstone-fire timers, keyed by hostname. A TCP drop
        # schedules one here; a reconnect within the grace window cancels
        # it before it ever fires, so the UI never shows "offline".
        self._pending_tombstones: dict[str, threading.Timer] = {}
        self._tombstone_lock = threading.Lock()

        self.on_local_change: list[Callable[[object], None]] = []
        self.on_peer_changed: list[Callable[[Peer], None]] = []
        self.on_peer_tombstoned: list[Callable[[Peer], None]] = []

        # Internal subscriber: fan local edits out to the mesh.
        self.on_local_change.append(self._broadcast_local)

    # ---------- commands the renderer invokes ----------

    def insert(self, s: str) -> None:
        self.document.insert(s)

    def delete_backward(self, n: int = 1) -> None:
        self.document.delete_backward(n)

    def delete_forward(self, n: int = 1) -> None:
        self.document.delete_forward(n)

    def move_cursor(self, line: int, col: int) -> None:
        self.document.move_cursor(line, col)

    def set_local_text(self, new_text: str) -> None:
        """Replace the local block. Used by renderers that drive editing
        natively (a Tk Text widget) and just mirror the result."""
        self.document.set_text(new_text)

    def save(self) -> None:
        self.document.save()

    def sorted_peers(self) -> list[Peer]:
        """Peers in display order - hostname, case-insensitive."""
        with self._peers_lock:
            return sorted(self.peers.values(), key=lambda p: p.hostname.lower())

    def local_snapshot(self) -> Snapshot:
        """A Snapshot of our current block, used by Mesh on new connections."""
        return Snapshot(content=self.document.text, seq=0)

    def start_networking(self) -> None:
        """Bind the mesh acceptor, register on the LAN, start discovering peers.

        Blocking - zeroconf's probe phase takes up to ~1.5s. The Tk
        renderer runs this on a background thread. Idempotent.
        """
        if self._discovery is not None:
            return

        self._mesh = Mesh(
            our_hostname_provider=lambda: self.hostname,
            local_snapshot_provider=self.local_snapshot,
            on_remote_message=self._on_remote_message,
            on_peer_disconnected=self._on_mesh_disconnect,
            listen_port=self._mesh_port,
        )
        self._mesh.start()

        self._discovery = Discovery(
            instance_name=self.hostname,
            mesh_port=self._mesh.listen_port,
            attach_port=self._attach_port,
            on_peer_changed=self._on_peer_discovered,
            on_peer_removed=self._on_peer_undiscovered,
        )
        self._discovery.start()
        if self._discovery.registered_name:
            self.hostname = self._discovery.registered_name

        self._periodic_stop.clear()
        self._periodic_thread = threading.Thread(
            target=self._periodic_loop, daemon=True
        )
        self._periodic_thread.start()

    def shutdown(self) -> None:
        """Persist local state and tear down networking. Idempotent."""
        self._periodic_stop.set()
        # Cancel any pending tombstone-fire timers so they don't fire after
        # shutdown and try to call back into a torn-down engine.
        with self._tombstone_lock:
            for timer in self._pending_tombstones.values():
                timer.cancel()
            self._pending_tombstones.clear()
        if self._mesh is not None:
            self._mesh.stop()
            self._mesh = None
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        self.document.save()

    # ---------- internal: local edit fanout ----------

    def _on_local_change(self, msg: object) -> None:
        for cb in self.on_local_change:
            cb(msg)

    def _broadcast_local(self, msg: object) -> None:
        if self._mesh is None:
            return
        if isinstance(msg, (Delta, Snapshot)):
            self._mesh.broadcast(msg)

    # ---------- internal: periodic re-broadcast ----------

    def _periodic_loop(self) -> None:
        """Every 30s, broadcast our current snapshot defensively. Cheap
        insurance against any silently-lost message."""
        while not self._periodic_stop.wait(PERIODIC_REBROADCAST_INTERVAL):
            if self._mesh is None:
                continue
            if self._mesh.connected_hostnames():
                self._mesh.broadcast(self.local_snapshot())

    # ---------- internal: discovery events ----------

    def _on_peer_discovered(self, dp: DiscoveredPeer) -> None:
        if dp.is_self:
            return
        with self._discovered_lock:
            self._discovered[dp.hostname] = dp
        # Discovery sees this peer again - cancel any pending tombstone
        # timer that was about to mark them offline.
        self._cancel_pending_tombstone(dp.hostname)
        with self._peers_lock:
            peer = self.peers.get(dp.hostname)
            if peer is None:
                peer = Peer(hostname=dp.hostname)
                self.peers[dp.hostname] = peer
            peer.address = dp.address
            peer.last_seen_ts = time.time()
            peer.tombstoned = False
        for cb in self.on_peer_changed:
            cb(peer)
        if self._mesh is not None:
            self._mesh.maybe_connect(dp)

    def _on_peer_undiscovered(self, hostname: str) -> None:
        """Discovery says this peer is gone from the LAN.

        Only tombstone if we *also* don't have a live TCP connection.
        mDNS announcements can flicker without the peer actually going
        away; TCP is the stronger liveness signal.
        """
        with self._discovered_lock:
            self._discovered.pop(hostname, None)
        if self._mesh is not None and self._mesh.is_connected(hostname):
            return
        with self._peers_lock:
            peer = self.peers.get(hostname)
            if peer is None:
                return
            peer.tombstoned = True
        for cb in self.on_peer_tombstoned:
            cb(peer)

    # ---------- internal: mesh events ----------

    def _on_remote_message(self, peer_hostname: str, msg: object) -> None:
        if peer_hostname == self.hostname:
            return
        # We're hearing from this peer again - cancel any pending tombstone
        # timer that was about to mark them offline. This is the
        # transient-blip suppression path: if the reconnect handshake
        # completes within the grace window, the UI never flickers offline.
        # Goodbye is the explicit "I'm leaving" message; for it we DO want
        # to tombstone immediately, so cancellation here is harmless either
        # way (we re-tombstone below).
        self._cancel_pending_tombstone(peer_hostname)
        with self._peers_lock:
            peer = self.peers.get(peer_hostname)
            if peer is None:
                peer = Peer(hostname=peer_hostname)
                self.peers[peer_hostname] = peer
            peer.last_seen_ts = time.time()
            peer.tombstoned = False
            if isinstance(msg, Snapshot):
                peer.block_text = msg.content
                peer.last_edit_ts = msg.ts
            elif isinstance(msg, Goodbye):
                peer.tombstoned = True
        for cb in self.on_peer_changed:
            cb(peer)
        if isinstance(msg, Goodbye):
            for cb in self.on_peer_tombstoned:
                cb(peer)

    def _on_mesh_disconnect(self, peer_hostname: str) -> None:
        """TCP connection dropped.

        Two things kick off here, both as ``threading.Timer`` callbacks:

        1. A reconnect attempt after RECONNECT_DELAY (2s).
        2. A tombstone-fire after ``self._tombstone_grace`` (default 5s).

        If the reconnect (or any other inbound message - heartbeat, etc.)
        lands before the tombstone timer fires, we cancel the tombstone
        timer in ``_on_remote_message`` and the UI never flickers offline.

        This trades up to ~5 extra seconds of "still showing as online
        after a real disconnect" for the much more common case of
        transient network blips not generating visible flicker.
        """
        with self._peers_lock:
            peer = self.peers.get(peer_hostname)
        # Schedule the actual tombstone-fire for later. Replace any
        # existing pending timer (e.g. drop / reconnect / drop in quick
        # succession) with a fresh one.
        if peer is not None:
            self._schedule_tombstone(peer_hostname)
        # Schedule the reconnect attempt independently. lex ordering
        # inside maybe_connect makes this a no-op on the side that was
        # supposed to accept rather than initiate.
        with self._discovered_lock:
            dp = self._discovered.get(peer_hostname)
        if dp is None or self._mesh is None:
            return
        mesh = self._mesh
        threading.Timer(
            RECONNECT_DELAY,
            lambda: self._safe_reconnect(mesh, dp),
        ).start()

    def _schedule_tombstone(self, peer_hostname: str) -> None:
        """Arrange for the tombstone callbacks to fire after the grace
        delay. Replaces any prior pending timer for this peer."""
        with self._tombstone_lock:
            prior = self._pending_tombstones.pop(peer_hostname, None)
            if prior is not None:
                prior.cancel()
            timer = threading.Timer(
                self._tombstone_grace,
                lambda: self._fire_tombstone(peer_hostname),
            )
            timer.daemon = True
            self._pending_tombstones[peer_hostname] = timer
            timer.start()

    def _cancel_pending_tombstone(self, peer_hostname: str) -> None:
        """Cancel a pending tombstone timer if there is one. Called when
        we hear from a peer again - either via mesh message or via
        re-discovery - so the UI never sees the offline flicker."""
        with self._tombstone_lock:
            pending = self._pending_tombstones.pop(peer_hostname, None)
        if pending is not None:
            pending.cancel()

    def _fire_tombstone(self, peer_hostname: str) -> None:
        """Tombstone-fire callback. Runs after the grace delay; no-ops if
        the peer has reconnected in the meantime (cancellation is the
        primary signal but we also re-check the live mesh state as a
        belt-and-braces guard against races between cancel and fire)."""
        if self._periodic_stop.is_set():
            return
        with self._tombstone_lock:
            self._pending_tombstones.pop(peer_hostname, None)
        # Belt-and-braces: if a Hello has landed since the timer was
        # scheduled, the connection is back and we should NOT tombstone.
        if self._mesh is not None and self._mesh.is_connected(peer_hostname):
            return
        with self._peers_lock:
            peer = self.peers.get(peer_hostname)
            if peer is None:
                return
            peer.tombstoned = True
        log_info(
            "tombstoning peer after grace period expired",
            context="engine.tombstone (" + str(peer_hostname) + ")",
        )
        for cb in self.on_peer_tombstoned:
            cb(peer)

    def _safe_reconnect(self, mesh: Mesh, dp: DiscoveredPeer) -> None:
        """Reconnect callback for the post-disconnect Timer. Wrapped so a
        crash inside maybe_connect doesn't kill the Timer thread silently."""
        if self._periodic_stop.is_set():
            return
        try:
            mesh.maybe_connect(dp)
        except Exception as e:
            log_exception(
                e,
                context="engine.reconnect (" + str(dp.hostname) + ")",
            )


__all__ = ["NetNotepad", "Peer", "Cursor", "DEFAULT_DATA_DIR"]
