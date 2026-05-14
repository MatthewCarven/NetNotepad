"""
Wire protocol for netnotepad.

Line-delimited JSON over TCP. Each message is one dataclass; the ``type``
field selects which one when decoding. Keep this module dependency-free
so it can be reasoned about in isolation and reused by tests.

Versioning: bump ``PROTOCOL_VERSION`` whenever the wire format changes.
The Hello message carries this value so peers can refuse to talk to
versions they don't understand.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Union


PROTOCOL_VERSION = 1


class MsgType(str, Enum):
    HELLO = "hello"
    DELTA = "delta"
    SNAPSHOT = "snapshot"
    HEARTBEAT = "heartbeat"
    ATTACHMENT_OFFER = "attach_offer"
    GOODBYE = "goodbye"


@dataclass
class Hello:
    """First message after a TCP connect. Identifies the peer."""

    hostname: str
    pid: int
    protocol_version: int = PROTOCOL_VERSION
    attachment_port: int = 47101
    type: str = MsgType.HELLO.value


@dataclass
class Delta:
    """One edit op applied to the sender's own block.

    Positions are grapheme-cluster indices into the sender's block,
    not byte offsets and not code-point offsets.
    """

    pos: int          # grapheme index where the op happens
    remove: int       # number of graphemes to delete starting at pos
    insert: str       # text to insert at pos (after the removal)
    seq: int          # monotonically increasing per sender; gaps signal drops
    ts: float = field(default_factory=time.time)
    type: str = MsgType.DELTA.value


@dataclass
class Snapshot:
    """Full block contents from the sender. Sent after Hello and on resync."""

    content: str
    seq: int          # the seq the snapshot reflects
    ts: float = field(default_factory=time.time)
    type: str = MsgType.SNAPSHOT.value


@dataclass
class Heartbeat:
    """Keep-alive. Refreshes last-seen but not last-edit."""

    ts: float = field(default_factory=time.time)
    type: str = MsgType.HEARTBEAT.value


@dataclass
class AttachmentOffer:
    """Announces a blob available for HTTP fetch from the sender."""

    sha256: str
    filename: str
    size: int
    mime: str
    type: str = MsgType.ATTACHMENT_OFFER.value


@dataclass
class Goodbye:
    """Graceful shutdown notice. Receivers tombstone the peer immediately."""

    type: str = MsgType.GOODBYE.value


Message = Union[Hello, Delta, Snapshot, Heartbeat, AttachmentOffer, Goodbye]


# ---------- encoding ----------


def encode(msg: Message) -> bytes:
    """Encode a message as one line of UTF-8 JSON (trailing newline included)."""
    return (json.dumps(asdict(msg)) + "\n").encode("utf-8")


def decode(line: bytes) -> Message:
    """Decode one JSON line into its dataclass. Raises ValueError on unknown type."""
    data = json.loads(line.decode("utf-8"))
    t = data.pop("type", None)
    if t == MsgType.HELLO.value:
        return Hello(**data)
    if t == MsgType.DELTA.value:
        return Delta(**data)
    if t == MsgType.SNAPSHOT.value:
        return Snapshot(**data)
    if t == MsgType.HEARTBEAT.value:
        return Heartbeat(**data)
    if t == MsgType.ATTACHMENT_OFFER.value:
        return AttachmentOffer(**data)
    if t == MsgType.GOODBYE.value:
        return Goodbye(**data)
    raise ValueError(f"Unknown message type: {t!r}")
