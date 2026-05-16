# TODO

## Up next

- [ ] **engine/attachments.py** - content-addressed blob store at `~/.netnotepad/attachments/<sha>`, tiny HTTP server on the attachment port, fetch-by-sha client, `AttachmentOffer` broadcasts on the keystroke channel. Tk integration: paste/drop a file or image to attach it; reference tokens in text expand to inline images via `text.image_create(...)`.
- [ ] **Terminal renderer using `prompt_toolkit`** - same engine API, multi-pane layout with our block editable and peer blocks read-only. Uses the engine's insert/delete/move_cursor methods rather than `set_local_text`, exercising the Delta path that Tk doesn't.

## Later

- [ ] Image attachment rendering in the Tk peers pane (currently shows the literal `![attachment:...]` token).
- [ ] Drag-and-drop file handling on the Tk window.
- [ ] Multi-block per peer (named panes within one peer, e.g. "scratch", "URLs").
- [ ] Periodic snapshot reconciliation - detect dropped `Delta` ops via `seq` gaps and resync.
- [ ] Cross-subnet / VPN-friendly discovery (multicast doesn't traverse subnets).
- [ ] CI matrix that builds Windows / macOS / Linux binaries via PyInstaller on GitHub Actions runners.
- [ ] Optional: encrypt the mesh channel with a shared LAN passphrase, for users who don't trust their LAN.

## Verified

- [x] **2026-05-13** - Document model unit tests pass (16/16).
- [x] **2026-05-13** - Discovery end-to-end smoke test passes - two Discovery instances find each other via mDNS.
- [x] **2026-05-13** - Network end-to-end tests pass (3/3) - two engines mutually discover, bidirectional sync works, late-joining peer receives initial snapshot.
- [x] **2026-05-13** - Full suite: 26/26 green.
- [x] **2026-05-14** - Diagnostic logging wired in. `error_handler.py` vendored into the package; new `netnotepad/log.py` exposes `log_info` / `log_exception` / `crash_to_file` / `set_log_dir`. Every silent except in the mesh, discovery, document, and Tk layers now routes through `log_exception(e, context=...)` into `~/.netnotepad/log.txt`. `__main__.py` wraps everything in an outer try/except that writes a `for_claude()` heavy report to `~/.netnotepad/last_crash.txt` on any unhandled crash. 9 new tests added in `tests/test_logging.py`. Full suite: 35/35 green.
- [x] **2026-05-14** - Tombstone grace period (5s) prevents UI flicker on transient network blips. TCP drop schedules a delayed tombstone instead of firing immediately; any inbound traffic during the grace window cancels it. Trade-off: a real disconnect takes ~5s longer to show as offline. Tests `test_tombstone_grace_suppresses_flicker_on_quick_reconnect` and `test_tombstone_fires_after_grace_when_no_reconnect` cover both paths. Full suite: 37/37 green.
- [x] **2026-05-14** - Windows packaging via PyInstaller. `run.py` entrypoint shim + `build_exe.bat` produces `dist\netnotepad.exe` as a single self-contained binary. Documented PATH placement (personal `%USERPROFILE%\bin`, not `C:\Windows`).
- [x] **2026-05-16** - Log rotation. `log.txt` capped at 1 MB (`LOG_MAX_BYTES`); on overflow it rolls to `log.1.txt` (single backup, prior backup overwritten). Implemented as `_maybe_rotate()` called under `_LOCK` at the head of `_append`. 4 new tests in `tests/test_logging.py` cover: rotation triggers at threshold, prior backup overwritten on second rotation, no rotation below threshold, no crash when log.txt doesn't exist yet. Full suite: 41/41 green.
- [x] **2026-05-16** - Apply remote Deltas to `peer.block_text`. Engine now applies incoming `Delta` messages via `_apply_delta_to_peer` (grapheme-aware, reuses `document._graphemes`, bounds-clamped defensively). Drops Deltas that arrive before the initial Snapshot for that peer (no authoritative base to build on); the 30s periodic Snapshot rebroadcast is the eventual-consistency fallback. 9 new tests in `tests/test_network.py` cover: pure insert / pure delete / replace / out-of-bounds pos clamp / out-of-bounds remove clamp / grapheme-cluster correctness (family-of-four emoji) / drop-before-snapshot / apply-after-snapshot / end-to-end propagation via `engine.insert` (exercises the Delta wire path that the future terminal renderer will use). Full suite: 50/50 green.
- [x] **2026-05-13** - First two-machine test (Matthew's LAN): instances on ID10TError-Laptop1 and DESKTOP-NM6GRPH discovered each other and exchanged initial snapshots, but live edits didn't propagate. Diagnosed as mDNS-flicker tombstones plus no auto-reconnect post-TCP-drop. Hardening pass applied:
    - Mesh.start() falls back to a kernel-assigned port if the requested one is taken (fixes "doesn't like being run more than once on the same PC").
    - Discovery-driven tombstone is suppressed when the TCP connection is still alive (mDNS flicker no longer marks live peers offline).
    - On TCP disconnect, the engine schedules a reconnect attempt after 2s using the last-known DiscoveredPeer; lex-order rules inside maybe_connect make this a no-op on the "should accept" side.
    - Heartbeat interval tightened to 5s; a watchdog thread closes any connection that hasn't received traffic for 25s, which then triggers the auto-reconnect path.
    - Periodic snapshot rebroadcast every 30s defensively catches any silently-dropped message.
    - All 26 existing tests still pass after the hardening pass.
