"""
LAN peer discovery via mDNS / Zeroconf.

Registers our service under ``_netnotepad._tcp`` and browses for peers of
the same type. Hostname collisions are handled by zeroconf's built-in
auto-rename (``host`` -> ``host-2`` -> ``host-3`` ...). The TXT record
carries: protocol version, attachment port, pid.

This module does NOT open TCP connections - that's the future
``peer.py`` / network-mesh layer. Discovery just tells you who's out
there. Callbacks fire on zeroconf's internal thread, so the consumer is
responsible for thread-safety (the engine holds a lock; the Tk renderer
queues events and drains them on the main loop).
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from zeroconf import (
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceListener,
    Zeroconf,
)

from netnotepad.log import log_exception
from netnotepad.protocol import PROTOCOL_VERSION


SERVICE_TYPE = "_netnotepad._tcp.local."
MESH_PORT = 47100
ATTACH_PORT = 47101


@dataclass
class DiscoveredPeer:
    """A peer we've found via mDNS. No TCP connection has been made yet."""

    hostname: str            # final registered name (may be auto-suffixed)
    address: str             # dotted-quad IPv4, or "" if unresolved
    mesh_port: int
    attach_port: int
    protocol_version: int
    pid: int
    is_self: bool = False    # True for our own service in browse results


def strip_service_suffix(full_name: str) -> str:
    """Convert ``host-2._netnotepad._tcp.local.`` -> ``host-2``.

    Pure helper, factored out for testability.
    """
    suffix = f".{SERVICE_TYPE}"
    if full_name.endswith(suffix):
        return full_name[: -len(suffix)]
    return full_name


def best_local_ip() -> str:
    """Pick a sensible LAN IP for advertising.

    Uses the UDP-connect trick: open a socket to a fake remote address,
    look at what local IP got picked. No packets are actually sent.
    Falls back to 127.0.0.1 if there's no network at all.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _parse_properties(props: dict) -> Optional[tuple[int, int, int]]:
    """Pull (version, attach_port, pid) out of a TXT record dict.

    Returns None if any field is missing or malformed - the peer is
    speaking a different dialect and we should skip them.
    """
    try:
        version = int(props.get(b"version", b"0").decode("ascii"))
        attach_port = int(props.get(b"attach_port", b"0").decode("ascii"))
        pid = int(props.get(b"pid", b"0").decode("ascii"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return version, attach_port, pid


class _BrowserListener(ServiceListener):
    """Adapter from zeroconf's add/remove/update callbacks to our types."""

    def __init__(self, owner: "Discovery"):
        self.owner = owner

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.owner._on_remove(name)

    def _resolve(self, zc: Zeroconf, name: str) -> None:
        info = zc.get_service_info(SERVICE_TYPE, name, timeout=2000)
        if info is None:
            return
        peer = self.owner._info_to_peer(name, info)
        if peer is not None:
            self.owner._on_add_or_update(peer)


class Discovery:
    """zeroconf registration + browse. Construct, then call ``start()``."""

    def __init__(
        self,
        instance_name: Optional[str] = None,
        mesh_port: int = MESH_PORT,
        attach_port: int = ATTACH_PORT,
        on_peer_changed: Optional[Callable[[DiscoveredPeer], None]] = None,
        on_peer_removed: Optional[Callable[[str], None]] = None,
    ):
        self._raw_name = instance_name or socket.gethostname()
        self._mesh_port = mesh_port
        self._attach_port = attach_port
        self._pid = os.getpid()
        self._on_peer_changed = on_peer_changed
        self._on_peer_removed = on_peer_removed

        self._zc: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._browser: Optional[ServiceBrowser] = None
        self._registered_name: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def registered_name(self) -> Optional[str]:
        """Final instance name after zeroconf collision resolution.

        ``None`` until ``start()`` has been called. May differ from the
        raw hostname if there was a name collision on the LAN.
        """
        return self._registered_name

    def start(self) -> None:
        """Register our service and start browsing. Blocking; returns once
        zeroconf has finished probing (usually under a second)."""
        self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        full_name = f"{self._raw_name}.{SERVICE_TYPE}"
        properties = {
            b"version": str(PROTOCOL_VERSION).encode("ascii"),
            b"attach_port": str(self._attach_port).encode("ascii"),
            b"pid": str(self._pid).encode("ascii"),
        }
        self._info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=full_name,
            port=self._mesh_port,
            properties=properties,
            parsed_addresses=[best_local_ip()],
            server=f"{self._raw_name}.local.",
        )
        # allow_name_change=True -> zeroconf auto-suffixes on collision.
        self._zc.register_service(self._info, allow_name_change=True)
        # After registration, .name reflects the final (possibly renamed) name.
        self._registered_name = strip_service_suffix(self._info.name)
        self._browser = ServiceBrowser(
            self._zc, SERVICE_TYPE, _BrowserListener(self)
        )

    def stop(self) -> None:
        """Unregister and close zeroconf. Idempotent."""
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._info is not None and self._zc is not None:
            try:
                self._zc.unregister_service(self._info)
            except Exception as e:
                # zeroconf can throw if the underlying socket is already
                # gone (e.g. dropped network adapter). Log for diagnostics
                # but proceed with close() regardless.
                log_exception(e, context="discovery.unregister")
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception as e:
                log_exception(e, context="discovery.close")
            self._zc = None

    # ---------- listener callbacks (called on zeroconf's thread) ----------

    def _info_to_peer(
        self, full_name: str, info: ServiceInfo
    ) -> Optional[DiscoveredPeer]:
        name = strip_service_suffix(full_name)
        parsed = _parse_properties(dict(info.properties or {}))
        if parsed is None:
            return None
        version, attach_port, pid = parsed
        addr = ""
        for raw in info.addresses or []:
            try:
                addr = socket.inet_ntoa(raw)
                break
            except OSError:
                continue
        is_self = (
            self._registered_name is not None
            and name == self._registered_name
        )
        return DiscoveredPeer(
            hostname=name,
            address=addr,
            mesh_port=info.port or self._mesh_port,
            attach_port=attach_port,
            protocol_version=version,
            pid=pid,
            is_self=is_self,
        )

    def _on_add_or_update(self, peer: DiscoveredPeer) -> None:
        if self._on_peer_changed is not None:
            self._on_peer_changed(peer)

    def _on_remove(self, full_name: str) -> None:
        if self._on_peer_removed is not None:
            self._on_peer_removed(strip_service_suffix(full_name))
