# NetNotepad — Design Notes

## Concept

A LAN-discoverable shared scratchpad. Each peer on the local network owns exactly one block of text. The full view is the concatenation of every peer's block, rendered in hostname-sort order. You can only edit your own block; everyone else's blocks are read-only mirrors.

## Why this shape

The region-per-peer model sidesteps the entire concurrent-editing problem. There is no OT, no CRDT, no conflict resolution — nobody is writing into anyone else's bytes. The cursor only ever moves through *our* block, so "snap to nearest valid position" only matters in a few narrow cases (load-from-disk, external truncation).

## Architecture

The codebase is split along a renderer/engine seam so the same engine can drive a Tk UI now and a terminal UI later without touching protocol or state logic.

- **`netnotepad/protocol.py`** — wire format. Line-delimited JSON over TCP. Messages: `Hello`, `Snapshot`, `Delta`, `Heartbeat`, `AttachmentOffer`, `Goodbye`. Dependency-free.
- **`netnotepad/engine/`** — headless core. Owns the local `Document`, remote peer mirrors, discovery, networking, attachments. Exposes a synchronous command API and emits events to subscriber lists.
- **`netnotepad/renderer/`** — UI. `tk_renderer.py` for v1; a `prompt_toolkit` terminal renderer is planned and will share the same engine surface.

## Identity & discovery

- mDNS / Zeroconf, service type `_netnotepad._tcp`.
- Service instance name = hostname; on collision, zeroconf auto-suffixes (`host-2`, `host-3`, ...).
- IP address comes for free from the service's A/AAAA records; we surface it dim in the per-peer header so two `Matthews-MacBook`s on the LAN are still distinguishable.
- TXT record carries: protocol version, attachment port, pid.

## Persistence

- Our own block saves to `~/.netnotepad/mine.txt` on a debounced schedule (~500ms after last keystroke) plus on graceful shutdown. Atomic via temp + rename.
- Peer blocks are in-memory only.
- Attachments cache to `~/.netnotepad/attachments/<sha256>` for the lifetime of the process.

## Tombstones

- A peer is "live" if last heartbeat <30s ago (or TCP-connected).
- A peer is "tombstoned" once heartbeat times out. Their last-known block stays visible, header dims, "(last seen 3:14pm)" appears.
- Tombstones persist for the lifetime of our process; they don't survive our own restart.

## Attachments

- Drag/paste a file or image to "attach" it to your block.
- The blob is content-addressed by SHA-256. Inlined in text as a token like `![attachment:<sha>:<filename>]`.
- Each peer runs a tiny HTTP server on a second port. Other peers fetch blobs from that server on demand. The keystroke channel never carries blob bytes.
- An `AttachmentOffer` message broadcasts when a new attachment is available, so peers can prefetch.

## Cursor model

- Tracked as `(line, col)` in **grapheme clusters**, scoped to our own block only.
- Grapheme awareness via the `regex` library's `\X` pattern. Falls back to per-code-point indexing if `regex` isn't installed (wrong for emoji-with-modifier but the package still imports).
- We don't track or display other peers' cursors.

## Versioning

- Protocol version int in every `Hello`. Future-us can break the wire format without bricking older instances; mismatched versions refuse to mesh.

## Not in v1

- Multi-block per peer. The data model could extend; the renderer would need named panes. Deliberately punted.
- Terminal renderer. Engine is decoupled so this drops in.
- Encryption / authentication. This is a trusted-LAN tool.
- VPN / cross-subnet discovery.

## What's keystroke-rate, what's not

- **Keystroke-rate (mesh channel):** `Delta` ops, `Heartbeat`, `AttachmentOffer`.
- **One-shot:** `Hello`, `Snapshot`, `Goodbye`.
- **Out of band (HTTP):** attachment blob bytes.

On a LAN this means typing fans out at a few hundred bytes per character, well below any threshold worth optimizing.
