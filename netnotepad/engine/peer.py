"""
Remote peer state and connection lifecycle.

STUB — not yet implemented. This module will own:
  * one ``PeerConnection`` per remote peer (TCP read/write loop)
  * applying remote ``Snapshot`` / ``Delta`` ops to ``Peer.block_text``
  * heartbeat timing and tombstone promotion
  * forwarding parsed messages up to the engine

See TODO.md for the next step.
"""
