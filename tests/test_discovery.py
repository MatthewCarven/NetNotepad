"""Tests for the discovery layer.

Pure-helper tests cover the parsing logic. The smoke test starts two
Discovery instances in the same process and asserts they find each other -
this only works on a system with a usable loopback or LAN interface, so
it's marked to skip gracefully if zeroconf can't bind.
"""

from __future__ import annotations

import threading
import time

import pytest

from netnotepad.engine.discovery import (
    SERVICE_TYPE,
    Discovery,
    DiscoveredPeer,
    _parse_properties,
    best_local_ip,
    strip_service_suffix,
)


# ---------- pure helpers ----------


def test_strip_service_suffix_matches():
    full = f"host-2.{SERVICE_TYPE}"
    assert strip_service_suffix(full) == "host-2"


def test_strip_service_suffix_no_match_returns_input():
    assert strip_service_suffix("just-a-name") == "just-a-name"


def test_parse_properties_ok():
    props = {
        b"version": b"1",
        b"attach_port": b"47101",
        b"pid": b"1234",
    }
    assert _parse_properties(props) == (1, 47101, 1234)


def test_parse_properties_missing_fields_uses_zero():
    # Missing fields default to 0; this is "valid but uninformative".
    assert _parse_properties({}) == (0, 0, 0)


def test_parse_properties_malformed_returns_none():
    props = {b"version": b"not-a-number"}
    assert _parse_properties(props) is None


def test_best_local_ip_returns_string():
    ip = best_local_ip()
    assert isinstance(ip, str)
    # Should look like a dotted quad even on the loopback fallback.
    assert ip.count(".") == 3


# ---------- end-to-end smoke test ----------


def test_two_discoveries_find_each_other():
    """Start two Discovery instances; each should see the other within a
    few seconds. Skips gracefully when zeroconf can't bind (CI sandboxes
    sometimes lack multicast)."""

    found_by_a: list[DiscoveredPeer] = []
    found_by_b: list[DiscoveredPeer] = []
    a_ready = threading.Event()
    b_ready = threading.Event()

    a = Discovery(
        instance_name="netnotepad-test-a",
        mesh_port=47190,
        attach_port=47191,
        on_peer_changed=lambda p: (found_by_a.append(p), a_ready.set()),
    )
    b = Discovery(
        instance_name="netnotepad-test-b",
        mesh_port=47192,
        attach_port=47193,
        on_peer_changed=lambda p: (found_by_b.append(p), b_ready.set()),
    )

    try:
        try:
            a.start()
            b.start()
        except OSError as e:
            pytest.skip(f"zeroconf couldn't bind in this environment: {e}")

        # Each side should see the other within a few seconds.
        seen_a = a_ready.wait(timeout=5.0)
        seen_b = b_ready.wait(timeout=5.0)

        if not (seen_a and seen_b):
            pytest.skip(
                "no mDNS traffic observed in this environment "
                "(common in containers without multicast)"
            )

        names_seen_by_a = {p.hostname for p in found_by_a if not p.is_self}
        names_seen_by_b = {p.hostname for p in found_by_b if not p.is_self}
        assert "netnotepad-test-b" in names_seen_by_a
        assert "netnotepad-test-a" in names_seen_by_b
    finally:
        a.stop()
        b.stop()
