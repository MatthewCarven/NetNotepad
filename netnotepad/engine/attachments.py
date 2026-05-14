"""
Attachment store and HTTP server for blob fetches.

STUB — not yet implemented. This module will:
  * hash incoming pastes/drops by SHA-256 and cache to
    ``~/.netnotepad/attachments/<sha>``
  * serve blobs over a tiny HTTP server on the attachment port
  * fetch remote blobs by sha from another peer's HTTP port
  * emit ``AttachmentOffer`` messages on the keystroke channel

Keystroke channel never carries blob bytes — only references.

See TODO.md for the next step.
"""
