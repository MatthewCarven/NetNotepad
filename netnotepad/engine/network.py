"""
TCP mesh for syncing peer blocks.

When Discovery surfaces a peer, the Mesh uses a lexicographic hostname
comparison to decide which side opens the TCP connection - exactly one
side connects, the other accepts. This prevents the classic P2P
double-connection race without needing a coordinator.

Once a connection is established both sides:
  1. Send Hello immediately on connect.
  2. Send a Snapshot of their current block right after Hello.
  3. Stream Delta/Snapshot ops on every local edit.
  4. Send Heartbeat every HEARTBEAT_INTERVAL seconds.

Each PeerConnection runs a dedicated reader thread; writes serialize
through a per-connection lock. A watchdog thread closes any connection
that hasn't received any traffic for ``HEARTBEAT_TIMEOUT`` seconds.

If the requested listen port is taken (common when two instances run on
the same machine), Mesh.start() falls back to a kernel-assigned port.
The engine picks up the actual bound port via ``listen_port`` and
advertises that via discovery's TXT record.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Callable, Optional

from netnotepad.engine.discovery import DiscoveredPeer, MESH_PORT
from netnotepad.log import log_exception, log_info
from netnotepad.protocol import (
    Delta,
    Goodbye,
    Heartbeat,
    Hello,
    Snapshot,
    decode,
    encode,
)


HEARTBEAT_INTERVAL = 5.0    # seconds between heartbeat broadcasts
HEARTBEAT_TIMEOUT = 25.0    # close conns silent for this long
WATCHDOG_INTERVAL = 2.0     # how often the watchdog checks
CONNECT_TIMEOUT = 5.0       # TCP connect timeout


class PeerConnection:
    """One TCP connection's worth of state. Owns one reader thread."""

    def __init__(
        self,
        sock: socket.socket,
        our_hostname: str,
        on_message: Callable[["PeerConnection", object], None],
        on_disconnect: Callable[["PeerConnection"], None],
    ):
        self.sock = sock
        self.our_hostname = our_hostname
        self.peer_hostname: Optional[str] = None
        self.last_received_ts: float = time.time()
        self._write_lock = threading.Lock()
        self._closed = False
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._reader: Optional[threading.Thread] = None

    def start(self) -> None:
        """Send our Hello and spawn the reader thread."""
        self.send(Hello(hostname=self.our_hostname, pid=os.getpid()))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def send(self, msg: object) -> None:
        if self._closed:
            return
        try:
            line = encode(msg)  # type: ignore[arg-type]
        except (TypeError, ValueError) as e:
            log_exception(e, context="mesh.send.encode")
            return
        with self._write_lock:
            try:
                self.sock.sendall(line)
            except OSError:
                # Sendall to a closed socket is a normal-disconnect signal,
                # not a bug to log. _do_close will fire the disconnect cb.
                self._do_close()

    def close(self) -> None:
        self._do_close()

    def _do_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._on_disconnect(self)

    def _read_loop(self) -> None:
        buf = b""
        try:
            while not self._closed:
                try:
                    chunk = self.sock.recv(8192)
                except OSError:
                    break
                if not chunk:
                    break
                self.last_received_ts = time.time()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = decode(line)
                    except (ValueError, json.JSONDecodeError) as e:
                        # Garbage on the wire from a peer running a different
                        # dialect, or partial data on a flaky link. Log once
                        # per malformed line and skip it.
                        log_exception(e, context="mesh.reader.decode")
                        continue
                    if isinstance(msg, Hello):
                        self.peer_hostname = msg.hostname
                    self._on_message(self, msg)
        finally:
            self._do_close()


class Mesh:
    """Manages PeerConnections. Accepts inbound + initiates outbound per lex order."""

    def __init__(
        self,
        our_hostname_provider: Callable[[], str],
        local_snapshot_provider: Callable[[], Optional[Snapshot]],
        on_remote_message: Callable[[str, object], None],
        on_peer_disconnected: Callable[[str], None],
        listen_port: int = MESH_PORT,
    ):
        self._our_hostname_provider = our_hostname_provider
        self._local_snapshot_provider = local_snapshot_provider
        self._on_remote_message = on_remote_message
        self._on_peer_disconnected = on_peer_disconnected
        self._listen_port = listen_port

        self._connections: dict[str, PeerConnection] = {}
        self._anon_connections: set[PeerConnection] = set()
        self._lock = threading.RLock()
        self._stopping = False

        self._listener_sock: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    @property
    def listen_port(self) -> int:
        return self._listen_port

    def connected_hostnames(self) -> set[str]:
        """Hostnames we currently have a live TCP connection to."""
        with self._lock:
            return set(self._connections.keys())

    def is_connected(self, hostname: str) -> bool:
        with self._lock:
            return hostname in self._connections

    def start(self) -> None:
        """Bind the acceptor and start the background threads. Falls back
        to a kernel-assigned port if the requested one is taken (common
        when two instances run on the same machine)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        requested = self._listen_port
        try:
            sock.bind(("0.0.0.0", requested))
        except OSError as e:
            log_exception(
                e, context="mesh.bind.fallback (requested=" + str(requested) + ")"
            )
            sock.bind(("0.0.0.0", 0))
        self._listen_port = sock.getsockname()[1]
        if self._listen_port != requested:
            log_info(
                "bound to fallback port " + str(self._listen_port)
                + " (requested " + str(requested) + ")",
                context="mesh.bind",
            )
        sock.listen(8)
        self._listener_sock = sock

        self._listener_thread = threading.Thread(
            target=self._accept_loop, daemon=True
        )
        self._listener_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        """Send Goodbye to everyone, close sockets, stop threads. Idempotent."""
        self._stopping = True
        with self._lock:
            conns = list(self._connections.values()) + list(self._anon_connections)
            self._connections.clear()
            self._anon_connections.clear()
        for c in conns:
            try:
                c.send(Goodbye())
            except Exception as e:
                log_exception(e, context="mesh.stop.goodbye")
            c.close()
        if self._listener_sock is not None:
            try:
                self._listener_sock.close()
            except OSError as e:
                log_exception(e, context="mesh.stop.listener_close")
            self._listener_sock = None

    def maybe_connect(self, dp: DiscoveredPeer) -> None:
        """Decide whether to initiate a connection to a discovered peer.

        Convention: only connect if their hostname > ours lex. The other
        side will, by the same rule, NOT connect to us and instead accept
        our connection. Exactly one TCP socket between any pair.
        """
        if dp.is_self or not dp.address:
            return
        our = self._our_hostname_provider()
        if dp.hostname <= our:
            return
        with self._lock:
            if dp.hostname in self._connections:
                return  # already wired up
            if self._stopping:
                return
        threading.Thread(
            target=self._connect_to, args=(dp,), daemon=True
        ).start()

    def broadcast(self, msg: object) -> None:
        """Send to every connected peer."""
        with self._lock:
            conns = list(self._connections.values())
        for c in conns:
            c.send(msg)

    # ---------- internals ----------

    def _connect_to(self, dp: DiscoveredPeer) -> None:
        try:
            sock = socket.create_connection(
                (dp.address, dp.mesh_port), timeout=CONNECT_TIMEOUT
            )
        except OSError as e:
            # Connect failed. Common during startup races, peer reboot,
            # firewall hiccup. Log so we can spot patterns over time.
            log_exception(
                e,
                context="mesh.connect (" + str(dp.hostname)
                + " @ " + str(dp.address) + ":" + str(dp.mesh_port) + ")",
            )
            return
        conn = PeerConnection(
            sock=sock,
            our_hostname=self._our_hostname_provider(),
            on_message=self._on_conn_message,
            on_disconnect=self._on_conn_disconnect,
        )
        with self._lock:
            self._anon_connections.add(conn)
        conn.start()

    def _accept_loop(self) -> None:
        while not self._stopping:
            sock = self._listener_sock
            if sock is None:
                break
            try:
                client, _addr = sock.accept()
            except OSError as e:
                # OSError on accept() is expected during shutdown (we close
                # the listener socket from another thread). Only log when
                # we weren't actively stopping.
                if not self._stopping:
                    log_exception(e, context="mesh.accept")
                break
            conn = PeerConnection(
                sock=client,
                our_hostname=self._our_hostname_provider(),
                on_message=self._on_conn_message,
                on_disconnect=self._on_conn_disconnect,
            )
            with self._lock:
                self._anon_connections.add(conn)
            conn.start()

    def _on_conn_message(self, conn: PeerConnection, msg: object) -> None:
        if isinstance(msg, Hello):
            self._promote(conn, msg.hostname)
        if conn.peer_hostname is not None:
            self._on_remote_message(conn.peer_hostname, msg)

    def _promote(self, conn: PeerConnection, peer_hostname: str) -> None:
        send_snapshot = False
        with self._lock:
            self._anon_connections.discard(conn)
            existing = self._connections.get(peer_hostname)
            if existing is not None and existing is not conn:
                conn.close()
                return
            self._connections[peer_hostname] = conn
            send_snapshot = True
        if send_snapshot:
            snap = self._local_snapshot_provider()
            if snap is not None:
                conn.send(snap)

    def _on_conn_disconnect(self, conn: PeerConnection) -> None:
        hostname = conn.peer_hostname
        with self._lock:
            self._anon_connections.discard(conn)
            if hostname is not None and self._connections.get(hostname) is conn:
                del self._connections[hostname]
        if hostname is not None:
            self._on_peer_disconnected(hostname)

    def _heartbeat_loop(self) -> None:
        while not self._stopping:
            slept = 0.0
            while slept < HEARTBEAT_INTERVAL and not self._stopping:
                time.sleep(0.1)
                slept += 0.1
            if self._stopping:
                return
            self.broadcast(Heartbeat())

    def _watchdog_loop(self) -> None:
        """Close any connection that hasn't received traffic in HEARTBEAT_TIMEOUT."""
        while not self._stopping:
            slept = 0.0
            while slept < WATCHDOG_INTERVAL and not self._stopping:
                time.sleep(0.1)
                slept += 0.1
            if self._stopping:
                return
            now = time.time()
            dead: list[PeerConnection] = []
            with self._lock:
                for conn in list(self._connections.values()):
                    if now - conn.last_received_ts > HEARTBEAT_TIMEOUT:
                        dead.append(conn)
            for c in dead:
                c.close()
