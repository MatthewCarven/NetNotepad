"""
Attachment store, blob server, and fetch client.

Attachments are content-addressed by SHA-256. The owning peer holds the
canonical bytes in ``~/.netnotepad/attachments/<sha>``; other peers
fetch by sha from that peer's tiny HTTP server on its attachment port.

The mesh (keystroke channel) never carries blob bytes. ``AttachmentOffer``
messages flow over the mesh as small references (sha, filename, size,
mime); the actual bytes ride out-of-band over HTTP. This keeps the
editing channel keystroke-rate even when someone drops in a 5 MB
screenshot.

In the text, an attachment is referenced by the token

    ![attachment:<sha>:<filename>]

The sha is the source of truth; the filename is for display only.

Module layout:
  * ``AttachmentStore``    - content-addressed blob cache on disk.
  * ``AttachmentServer``   - tiny HTTP server that serves blobs from a store.
  * ``fetch_blob``         - urllib client that downloads + verifies a blob.
  * ``parse_attachment_tokens`` / ``make_attachment_token`` - text helpers.

Matches Mesh's fallback-port pattern: if the requested attachment port
is taken, AttachmentServer falls back to a kernel-assigned port and
exposes the actual port via ``listen_port`` so discovery can advertise
it in its TXT record.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from netnotepad.engine.discovery import ATTACH_PORT
from netnotepad.log import log_exception, log_info


# Hard cap for a single attachment, in bytes. Above this we refuse to
# attach (locally) or to finish a fetch (remotely) rather than try to
# ship a 1 GB blob through a tiny HTTP server. 50 MB covers screenshots,
# PDFs, and small archives without inviting abuse.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# How long to wait on the fetch socket. LAN-tight; we'd rather fail fast
# than hang the UI on a flapping link.
FETCH_TIMEOUT = 10.0

# Read chunk size for streaming bytes off the wire and off disk.
CHUNK = 64 * 1024

# Regex matching the inline attachment token. The sha group is anchored
# to exactly 64 lowercase hex chars; filename is everything up to the
# next ``]``. Newlines, tabs, and ``]`` are scrubbed at insert time, so
# a filename inside the token cannot smuggle in line breaks.
_TOKEN_RE = re.compile(r"!\[attachment:([0-9a-f]{64}):([^\]]*)\]")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def guess_mime(filename: str) -> str:
    """Best-effort MIME from the filename extension. Falls back to
    ``application/octet-stream`` so we always have *some* string to put
    in the AttachmentOffer."""
    mt, _ = mimetypes.guess_type(filename)
    return mt or "application/octet-stream"


def make_attachment_token(sha: str, filename: str) -> str:
    """Build the inline token that goes in our block text.

    Format: ``![attachment:<sha>:<filename>]``. The filename is for
    display only; the sha is the source of truth.

    Strips characters that would break the token (newlines, tabs, ``]``)
    from the filename so the token survives round-tripping through the
    text widget unmodified.
    """
    safe = "".join(c for c in filename if c not in "\r\n\t[]")
    return "![attachment:" + sha + ":" + safe + "]"


def parse_attachment_tokens(text: str) -> list[tuple[str, str, int, int]]:
    """Find every attachment token in ``text``.

    Returns a list of ``(sha, filename, start, end)`` tuples where
    ``start`` / ``end`` are character offsets into ``text``. Used by the
    renderer to know what to highlight and to look up blobs for
    inline-render or save-as.
    """
    out: list[tuple[str, str, int, int]] = []
    for m in _TOKEN_RE.finditer(text):
        out.append((m.group(1), m.group(2), m.start(), m.end()))
    return out


# ----------------------------- storage -----------------------------


class AttachmentStore:
    """Content-addressed blob cache rooted at a directory.

    A blob's identity is its sha256 hex digest. ``put_bytes`` / ``put_file``
    return that sha; subsequent ``has`` / ``path_for`` / ``read`` calls
    look it up by sha. Writes are atomic (temp file + rename) so a
    crashed write never leaves a half-written blob at the canonical name.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str) -> Path:
        return self.root / sha

    def has(self, sha: str) -> bool:
        return self.path_for(sha).is_file()

    def size(self, sha: str) -> int:
        return self.path_for(sha).stat().st_size

    def read(self, sha: str) -> bytes:
        return self.path_for(sha).read_bytes()

    def put_bytes(self, data: bytes) -> str:
        """Hash + store ``data``; return its sha. No-op if already cached."""
        sha = _sha256_bytes(data)
        target = self.path_for(sha)
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)  # atomic on POSIX and Windows
        return sha

    def put_file(self, path: Path) -> str:
        """Hash + store the contents of ``path``; return its sha."""
        data = Path(path).read_bytes()
        return self.put_bytes(data)

    def save_to(self, sha: str, dest: Path) -> None:
        """Copy the cached blob to ``dest`` (a user-chosen location).

        Raises FileNotFoundError if the sha isn't cached yet, so callers
        can prompt for a fetch first.
        """
        src = self.path_for(sha)
        if not src.is_file():
            raise FileNotFoundError(sha)
        Path(dest).write_bytes(src.read_bytes())


# ----------------------------- HTTP server -----------------------------


def _make_handler(store: AttachmentStore):
    """Build a BaseHTTPRequestHandler class bound to a specific store.

    Returns a class (not an instance) because http.server instantiates
    one handler per request.
    """

    class _Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log; route errors through our
        # diagnostic logger instead so they end up in ~/.netnotepad/log.txt
        # alongside everything else.
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            # Route: /blob/<sha>. Anything else is 404.
            parts = self.path.split("/")
            if len(parts) != 3 or parts[0] != "" or parts[1] != "blob":
                self.send_error(404, "not found")
                return
            sha = parts[2]
            # Validate sha shape before touching the filesystem.
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                self.send_error(400, "bad sha")
                return
            if not store.has(sha):
                self.send_error(404, "unknown blob")
                return
            try:
                size = store.size(sha)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with store.path_for(sha).open("rb") as f:
                    while True:
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (OSError, ConnectionError) as e:
                # Client hung up mid-send is normal on the LAN (user closed
                # a window, switched apps). Log so we can spot patterns but
                # don't crash the server thread.
                log_exception(e, context="attach_server.serve")

    return _Handler


class AttachmentServer:
    """Tiny single-port HTTP server serving blobs from an AttachmentStore.

    Mirrors Mesh's fallback-port behaviour: if the requested port is
    taken, fall back to a kernel-assigned port and expose the actual
    bound port via ``listen_port`` so discovery can advertise it.
    """

    def __init__(self, store: AttachmentStore, port: int = ATTACH_PORT):
        self._store = store
        self._requested_port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._listen_port = 0

    @property
    def listen_port(self) -> int:
        """The actual bound port. ``0`` until ``start()`` has run."""
        return self._listen_port

    def start(self) -> None:
        handler = _make_handler(self._store)
        try:
            server = ThreadingHTTPServer(
                ("0.0.0.0", self._requested_port), handler
            )
        except OSError as e:
            log_exception(
                e,
                context="attach_server.bind.fallback (requested="
                + str(self._requested_port) + ")",
            )
            server = ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self._server = server
        self._listen_port = server.server_address[1]
        if (
            self._listen_port != self._requested_port
            and self._requested_port != 0
        ):
            log_info(
                "attachment server bound to fallback port "
                + str(self._listen_port)
                + " (requested " + str(self._requested_port) + ")",
                context="attach_server.bind",
            )
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the server. Idempotent.

        ``shutdown()`` must run on a thread different from the one running
        ``serve_forever`` or it deadlocks. We're always called from the
        engine thread; serve_forever runs in ``self._thread``. Safe.
        """
        srv = self._server
        if srv is None:
            return
        try:
            srv.shutdown()
        except OSError as e:
            log_exception(e, context="attach_server.shutdown")
        try:
            srv.server_close()
        except OSError as e:
            log_exception(e, context="attach_server.close")
        self._server = None
        self._thread = None


# ----------------------------- fetch client -----------------------------


def fetch_blob(
    address: str,
    port: int,
    sha: str,
    store: AttachmentStore,
    timeout: float = FETCH_TIMEOUT,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> bool:
    """Download a blob from a peer's attachment server and stash it.

    Returns True if the blob is present in ``store`` after the call
    (either because it was already cached or because we just downloaded
    it). Returns False on any failure - network error, 404, exceeding
    ``max_bytes``, or sha mismatch. Logs diagnostic details on the
    failure paths so a flapping fetch leaves a trail.
    """
    if store.has(sha):
        return True
    url = "http://" + address + ":" + str(port) + "/blob/" + sha
    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    log_info(
                        "blob exceeds max_bytes (" + str(max_bytes)
                        + "); aborting fetch",
                        context="attach.fetch (" + str(sha) + ")",
                    )
                    return False
                hasher.update(chunk)
                chunks.append(chunk)
    except (urllib.error.URLError, OSError) as e:
        log_exception(
            e,
            context="attach.fetch (" + str(sha)
            + " @ " + str(address) + ":" + str(port) + ")",
        )
        return False
    actual = hasher.hexdigest()
    if actual != sha:
        log_info(
            "blob sha mismatch (got " + actual + ", expected " + sha + ")",
            context="attach.fetch",
        )
        return False
    # Land bytes through the store's atomic put for a single safe-write path.
    store.put_bytes(b"".join(chunks))
    return True


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "FETCH_TIMEOUT",
    "AttachmentStore",
    "AttachmentServer",
    "fetch_blob",
    "guess_mime",
    "make_attachment_token",
    "parse_attachment_tokens",
]
