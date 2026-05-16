# Worklog

## 2026-05-13 - scaffold, Tk renderer, discovery, TCP mesh, peer view

Design captured in `DESIGN.md`. By end of day the app has: a working LAN mesh that auto-discovers peers via zeroconf, syncs block contents over TCP on every keystroke, persists locally to `~/.netnotepad/mine.txt`, and renders both your own pane and all peers' panes in a single Tk window. Two instances on a LAN (or same machine in different processes) now actually copy text between each other in near-real-time.

### Architecture

The codebase is split along a renderer / engine seam so the same engine can drive a Tk UI today and a terminal UI later without protocol or state code changing shape.

- `netnotepad/protocol.py` - wire format. Six message dataclasses (`Hello`, `Snapshot`, `Delta`, `Heartbeat`, `AttachmentOffer`, `Goodbye`), JSON encode/decode, dependency-free.
- `netnotepad/engine/document.py` - local block model. Grapheme-aware cursor via `regex`'s `\X`, atomic save, load-on-init. `set_text(new)` for renderer-driven editing (emits Snapshot); `insert` / `delete_backward` / `delete_forward` / `move_cursor` for engine-driven editing (emit Deltas).
- `netnotepad/engine/discovery.py` - zeroconf register + browse. Auto-rename on collision, TXT record carries version/attach_port/pid.
- `netnotepad/engine/network.py` - TCP mesh. Lex-ordered "their_name > our_name -> we connect" convention prevents double-connections. `PeerConnection` runs a reader thread per socket; writes serialize through a per-connection lock. Heartbeat broadcast every 10s. Promote-on-Hello flow sends a snapshot of our current block to every new peer.
- `netnotepad/engine/__init__.py` - `NetNotepad` facade. Wires document, discovery, mesh together. Public surface: `insert` / `delete_*` / `move_cursor` / `set_local_text` / `save` / `start_networking` / `shutdown` plus subscriber lists (`on_local_change`, `on_peer_changed`, `on_peer_tombstoned`).
- `netnotepad/renderer/tk_renderer.py` - single-window Tk UI. Top status bar (hostname · peer count · transient state), middle editable pane (our block), bottom read-only pane (every peer's block, hostname-sorted, dimmed if tombstoned). Threading: networking starts in background; zeroconf/mesh callbacks bounce onto Tk via `root.after(0, ...)`.
- `netnotepad/__main__.py` - `python -m netnotepad`, renderer selectable via `--renderer tk|term`.

### Tests (26 passing)

- `tests/test_document.py` - 16 tests covering insert/delete/cursor clamping/newline tracking/delta emission/persistence/set_text snapshot path.
- `tests/test_discovery.py` - 7 tests: pure helpers plus an end-to-end smoke test that boots two Discovery instances and confirms they see each other via real zeroconf traffic.
- `tests/test_network.py` - 3 end-to-end tests on full `NetNotepad` engines: mutual discovery, bidirectional edit propagation, initial-snapshot on late-joining peer.

### Design adjustments made during implementation

1. `Document.set_text(new)` emits Snapshot instead of Delta. Tk owns its native editing UX (selection, paste, IME, undo); the engine mirrors the result. Insert/delete methods stay for the future terminal renderer where editing flows through the engine.
2. Engine accepts an `instance_name` parameter so tests can pin hostnames without monkeypatching `socket.gethostname`.
3. Test ports use `mesh_port=0` (kernel-assigned) so they don't collide with Linux's ephemeral port range (32768-60999).
4. Tombstone semantics: a peer is tombstoned on either (a) zeroconf removal or (b) TCP disconnect or (c) explicit Goodbye. Heartbeat-timeout tombstones are on the TODO.

### Running it

`pip install regex zeroconf`, then `python -m netnotepad`. Run it on two machines on the same LAN (or two terminals on one machine) and the bottom pane fills in with the other peer's text in near real-time as they type. Close one window and the other shows "(1 offline)" with the disconnected peer's last-known block dimmed.

Process note: hit a filesystem-sync issue early on where the file tools and the bash mount went out of sync, leaving truncated content visible only to bash. Worked around by writing critical files via bash heredocs. Worth keeping in mind for future sessions.

## 2026-05-14 - diagnostic logging via error_handler

Vendored the `error_handler.py` Matthew wrote in a separate co-work into the `netnotepad/` package and built a thin logging shim around it. The point is post-mortem visibility: when something quietly dies on one of the two LAN machines, there's now a paper trail rather than a screen full of "well, it just stopped working".

### New files

- `netnotepad/error_handler.py` - copy of the standalone `describe_error()` introspector. `to_string()`, `to_dict()`, `for_claude()` flavors; never raises; type-specific extractors for OSError / SyntaxError / AttributeError / KeyError / UnicodeError; cycle-safe cause/context chain walking; truncating safe-repr for frame locals.
- `netnotepad/log.py` - thread-safe shim with three public calls:
  - `log_info(msg, context="")` - timestamped INFO line into `~/.netnotepad/log.txt`.
  - `log_exception(exc, context="")` - timestamped ERROR header + `describe_error().to_string()` body, indented for scanability, into the same file. Never raises.
  - `crash_to_file(exc)` - writes the heavy `for_claude()` flavor (with `include_locals=True`) to `~/.netnotepad/last_crash.txt`. The "paste this at Claude" path.
  - `set_log_dir(path)` - pointed at the engine's data dir during `NetNotepad.__init__`, so it auto-tracks wherever the user told the engine to live.

### Wiring

Every silent `except: pass` in the engine layers now routes through `log_exception` with a context tag:

- `network.py`: `mesh.send.encode`, `mesh.reader.decode`, `mesh.bind.fallback`, `mesh.stop.goodbye`, `mesh.stop.listener_close`, `mesh.connect`, `mesh.accept`.
- `discovery.py`: `discovery.unregister`, `discovery.close`.
- `engine/__init__.py`: `engine.reconnect` (wrapping the post-disconnect Timer lambda - previously a bare lambda, now `_safe_reconnect` so a crash inside `maybe_connect` can't kill the Timer thread silently).
- `document.py`: `document.load`.
- `tk_renderer.py`: `tk.start_networking`, `tk.on_close.save`, `tk.on_close.shutdown`.

Sockety errors that are part of normal disconnect flow (sendall on a closed sock, recv returning empty, accept during shutdown) are NOT logged - they'd just be noise. Only surprises hit the log.

`__main__.py` got an outer `_entrypoint()` that wraps `main()` with a final try/except: `KeyboardInterrupt` exits 0, `SystemExit` re-raises, anything else writes both an entry to `log.txt` and a full `for_claude()` report to `last_crash.txt`, prints the path to stderr, and exits 1. That's the user-facing crash-report path - if netnotepad ever dies hard, you get a single text file you can paste into a Claude chat.

### Tests (35 passing - was 26, +9 new)

- `tests/test_logging.py` - 9 tests:
  - `log_info` writes a line with context tag and `[INFO ]` marker.
  - `log_exception` writes the full traceback + exception type + message.
  - `log_exception` works with no context tag too.
  - `crash_to_file` writes the heavy/labeled flavor to `last_crash.txt`.
  - A deliberate raise inside a background thread, routed via `log_exception`, ends up in `log.txt` (this is the real-world usage pattern in mesh threads).
  - 20 concurrent threads writing exception entries don't corrupt each other's lines (verifies the module-level lock).
  - `log_exception` doesn't raise even when `open()` is monkeypatched to raise (file system perms / disk full survival).
  - `set_log_dir` changes destination cleanly with no cross-bleed.
  - `get_log_dir` defaults to `~/.netnotepad` when unset.

### What this unlocks

Next time the laptop and desktop go quiet, you can `type ~/.netnotepad/log.txt` (or just open it in netnotepad itself, eventually) and see exactly which socket dropped when, which decode failed, which port the mesh fell back to. And if either side crashes outright, `last_crash.txt` is a ready-to-paste report.

Still pending: the actual two-machine re-test of the connection-stability hardening pass from 2026-05-13. The logging changes here are orthogonal to that and don't affect mesh behavior - they only observe it.

## 2026-05-14 (later) - tombstone grace period + Windows packaging

Two small but visible-effect changes:

### Tombstone grace period

Matthew flagged that on his weaker box (Meatthread0), peers occasionally flicker offline for a few seconds before returning. Diagnosis: the engine was tombstoning instantly on any TCP drop, even when the reconnect would land within 2-3 seconds. Sub-second WiFi hiccups, GC pauses on the peer, antivirus grabbing a thread - any of these would trigger a visible offline blink before the auto-reconnect path put things right.

Fix: added `TOMBSTONE_GRACE_DELAY = 5.0` in `engine/__init__.py`. The flow is now:

1. TCP drops → `_on_mesh_disconnect` fires.
2. We schedule a 5s tombstone-fire timer instead of tombstoning immediately.
3. We schedule the existing 2s reconnect attempt in parallel.
4. If ANY inbound message (Hello, Heartbeat, Snapshot, anything) lands before the 5s mark, `_on_remote_message` cancels the pending tombstone via `_cancel_pending_tombstone`. UI never sees offline.
5. If the 5s elapses with no traffic, `_fire_tombstone` runs - belt-and-braces re-checks `mesh.is_connected` and only THEN sets `peer.tombstoned = True` and fires the callbacks.

`shutdown()` now also cancels all pending tombstone timers so they can't fire into a torn-down engine.

Engine constructor gained a `tombstone_grace` parameter (default 5.0) so tests can pass tiny values for fast turnaround. Two new tests in `test_network.py` exercise both paths:
- `test_tombstone_grace_suppresses_flicker_on_quick_reconnect` - drop + quick reconnect → `on_peer_tombstoned` never fires.
- `test_tombstone_fires_after_grace_when_no_reconnect` - drop + silence → fires exactly once after the grace window.

Both run with `tombstone_grace=0.15` or `0.2` so they finish in well under a second each.

Trade-off: a peer that really did go offline now takes ~5 extra seconds to show as offline. For an interactive scratchpad that's the right side of the trade - users notice flicker; they don't notice "still shows online for 5 more seconds after they truly left".

### Windows packaging

Added `run.py` (a tiny PyInstaller entrypoint shim that forwards to `netnotepad.__main__._entrypoint`) and `build_exe.bat` (one-double-click PyInstaller build). Output is `dist\netnotepad.exe`, a single self-contained ~15-25MB binary that drops anywhere on PATH.

Used `--onefile --windowed --collect-all zeroconf`. The `--collect-all` is because zeroconf imports some submodules dynamically and PyInstaller's static analyzer misses them otherwise.

### Tests (37 passing - was 35, +2 for grace period)

Full suite green: 16 document, 7 discovery, 5 network (incl. 2 new grace), 9 logging.

### Repo cleanup for git init

Expanded `.gitignore` to cover PyInstaller artifacts (`build/`, `dist/`, `*.spec`), pytest cache, IDE droppings, OS junk (Thumbs.db, .DS_Store, $RECYCLE.BIN), and the runtime files netnotepad writes if you ever run it from the repo dir (`mine.txt`, `log.txt`, `last_crash.txt`).

Matthew is about to push the repo somewhere public, so this seemed like a good moment to make sure we're not committing any of that.

### README.md + LICENSE.md

Added a GitHub-facing `README.md`: tagline + "the itch" motivation, features list, install/run (`pip install regex zeroconf` + `python -m netnotepad`, or double-click `start.bat`), how-it-works summary (engine/renderer split, lex-ordered mesh, 5s heartbeat / 25s watchdog), project layout tree, Windows .exe build instructions (double-click `build_exe.bat`, drop in `%USERPROFILE%\bin`, NOT `C:\Windows`), test count (37 passing), license pointer, acknowledgements (credits Claude/Cowork and notes `error_handler.py` came from a separate Cowork session).

License link points at `LICENSE.md` (uppercase, `.md`) to match the YPilot license file Matthew is copying in.

End-of-day state: 37/37 tests passing, repo ready for `git init` + public push.

## 2026-05-16 - resync vendored error_handler.py

The standalone `error_handler.py` at the project root grew quite a bit since the original vendoring (Matthew kept iterating on it in a separate Cowork session). The vendored copy at `netnotepad/error_handler.py` was now ~731 lines behind the 1374-line standalone. None of it was breaking the running app — `netnotepad/log.py` only calls `describe_error(exc).to_string()` and `describe_error(exc, include_locals=True).for_claude()`, and the two kwargs it uses (`exc`, `include_locals`) have identical semantics in both versions — but the newer features were worth picking up.

### What landed in the vendored copy

Copied root `error_handler.py` over `netnotepad/error_handler.py` verbatim, then patched the docstring (corrected the import-path example to `from netnotepad.error_handler import describe_error`, removed a stale `(stub)` parenthetical on the `for_claude` line, and re-added the "this is a copy of the standalone, keep them in sync" footer that the vendored copy carries).

New capabilities the running app now inherits without any changes to `log.py`:

- **Source-context windows** — every traceback frame in both `to_string()` and `for_claude()` now gets a 7-line window (3 lines either side of the error line), dedented for legibility, with line numbers and a `>>` marker on the error line itself. Massive readability win for crash dumps.
- **Caller context** — frames *above* the catch site are walked and rendered too, nearest-to-oldest, with the same source-context treatment. Capped at 32 frames with a truncation marker.
- **ExceptionGroup support** — proper handling of Python 3.11+ `BaseExceptionGroup` (and the 3.10 `exceptiongroup` backport via duck-typing). Each child exception gets full introspection, with cycle and nesting-depth guards.
- **Environment snapshot** — Python version, platform, system, machine, cwd, pid, argv, executable. Renders into the heavy/for_claude edition as an `ENVIRONMENT` block.
- **Redaction subsystem** — `register_redactor()`, `redact_pattern()`, `clear_redactors()` public helpers + an internal `_redact` that runs on every captured string (locals, source, messages, args reprs, notes, env-var values). ContextVar-based so concurrent `describe_error` calls don't stomp on each other. Not wired up to anything yet — available for future use.
- **`redactors=`, `caller_context=`, `max_caller_frames=`, `max_group_depth=`, `environment_snapshot=`, `env_vars=`, `source_context_lines=` kwargs** — all optional, all defaulting on (except redactors and env_vars), so no `log.py` change needed to benefit.

### Tests

`pytest -q` from project root: **37/37 passing** in ~13s. No test changes needed — the vendored module's public surface (`describe_error`, `ErrorReport`) is a strict superset of what it was before, and all existing kwargs behave identically.

### Smoke-tested live

Wrote a tiny script that raises through a two-frame call chain (`deep` → `shallow` → `int("not a number")`), caught it, ran `describe_error(e).to_string()`. Confirmed the output now includes:

- Source-context windows under each traceback frame, with `>>` on the error line.
- A `Caller context` block showing the frame above the catch.

Older crash output was the bare three-line traceback; new crash output gives the LLM (or you) the surrounding code on every frame. Worth the resync.

### Files touched
- `netnotepad/error_handler.py` — 731 → 1378 lines (full replacement + docstring patch).
- Project-root `error_handler.py` left untouched (it's the source of truth).
- `netnotepad/log.py` — **no changes**. The new richer output is purely additive on defaults.

## 2026-05-16 (later) - sticky vertical scrollbars on Tk panes

Matthew asked for a vertical scrollbar on the text in netnotepad, and specifically wanted it to be "sticky" — i.e., stay put across updates rather than jumping back to the top whenever a peer's content refreshes.

### What changed in `tk_renderer.py`

Both text widgets are now wrapped in a container `Frame` with a `tk.Scrollbar` packed on the right. The scrollbar is wired up via `text.configure(yscrollcommand=scroll.set)` and `Scrollbar(command=text.yview)` in the usual Tk pattern.

- **Editable pane** (top): scrollbar added; no other behavioral change. Tk already preserves scroll position naturally across user edits.
- **Peers pane** (bottom): scrollbar added, **plus** sticky-scroll logic in `refresh_peers_view()`. Before the `delete("1.0", "end")` that wipes the pane, we capture `peers_view.yview()[0]` (the top-of-viewport as a fraction 0.0..1.0). After rebuilding the content we call `peers_view.yview_moveto(saved_top)` to restore it. Both calls are wrapped in `try/except tk.TclError` for paranoia.

The sticky behaviour matters specifically for the peers pane because `refresh_peers_view()` runs `delete("1.0", "end")` and reinserts everything from scratch on every peer event (Snapshot, Delta, tombstone, peer join/leave). Without the save/restore, the viewport would jump back to the top whenever any peer typed a character — actively annoying when you're trying to read what someone wrote three peers down.

Edge cases handled:
- **Content shrinks** (peer goes offline, fewer rows than before) — `yview_moveto` clamps to a valid range, no error.
- **First update before pane is laid out** — `yview()` can raise `tk.TclError` if called before geometry is settled; we catch and default to 0.0.
- **User scrolled to the very bottom** — they stay at the bottom (because the fraction is whatever the bottom resolves to after rebuild). No special "follow new content" logic — Matthew specifically said "stay where it is", so we just preserve the literal position.

### Tests

`pytest -q` from project root: still **37/37 passing**. No new tests — the Tk renderer is not covered by the existing test suite (it's UI/event-loop code; the engine tests are headless), and adding a Tk-driving test for a behaviour you can verify in two seconds by running the app felt like overengineering for a hobbyist project. Compile-only check: `python -m py_compile netnotepad/renderer/tk_renderer.py` clean.

### Files touched
- `netnotepad/renderer/tk_renderer.py` — two `Frame` wrappers + two `Scrollbar` widgets + `yview` save/restore in `refresh_peers_view`. ~25 added lines.

## 2026-05-16 (later still) - surgical per-peer peers-pane updates

The sticky-scroll from earlier (saving `yview()[0]` as a fraction across the wipe-and-rebuild) helped, but it still jumped a bit because the fraction is content-relative: as soon as content above the viewport grows or shrinks (any peer typing), the same fraction now points to a different spot. Matthew correctly diagnosed the underlying problem — we were redrawing things that hadn't changed.

### The fix: don't redraw what didn't change

Refactor `refresh_peers_view` from "wipe the pane, rebuild everything" to "diff and patch":

- **Each peer's section is bracketed by two Tk marks** — `__peer_start_<hostname>` with left gravity, `__peer_end_<hostname>` with right gravity. Marks are positions in a Text widget that Tk auto-shifts as content is inserted or deleted *around* them. Left-gravity on start means inserts at that position go to its right; right-gravity on end means inserts at that position go to its left. Net effect: we can `delete(start_mark, end_mark)` and reinsert content, and the brackets re-bracket the new content correctly. More importantly: when peer A's section is rewritten, the start/end marks of peers B and C automatically adjust their absolute line numbers to keep pointing at the same content — without us touching their sections at all.

- **Per-peer fingerprint cache** — for each peer we record a tuple `(address, last_edit_ts, last_seen_ts, tombstoned, block_text)`. On refresh, if a peer's current fingerprint matches the cached one, that peer's section is left completely untouched.

- **Viewport-top anchor mark** — before any surgical updates, set a mark `__viewport_top` at `index("@0,0")`. Tk's mark adjustment means that even if content above the viewport changes size, this mark continues to point at the same content. After updates, `yview("__viewport_top")` scrolls so that content is back at the top.

### Dispatch

`refresh_peers_view` now branches:

- **Surgical path** (the common case — peer set and sort order unchanged): only redraw sections whose fingerprint changed. If nothing changed at all, it's a no-op.
- **Full-rebuild path**: a peer joined, left, or got reordered. Falls back to `_full_rebuild_peers_view`, which is essentially the old wipe-and-rebuild but with the per-peer-anchor sticky-scroll logic from the earlier round (capture which peer was at the top + line offset before the wipe; after the rebuild, scroll to that peer's new header + offset).

Initial paint goes through the full-rebuild path explicitly (replacing the bare `refresh_peers_view()` call before `mainloop`), so the "no peers yet" placeholder draws on startup and the section marks get created for any peers already present.

### What this means for daily use

Steady-state behaviour with three peers on the LAN:

- Peer A types one character → Peer A's section gets `delete + reinsert`, body content morphs in place. Peers B and C's sections are not touched at all (no delete, no insert, no tag operations). The viewport is rock-still.
- Peer B comes online → falls into full-rebuild path. One-time sticky-scroll restore: the user stays anchored to whoever they were reading.
- A spurious peer event with no actual content change (e.g. a discovery refresh that didn't change address/edit-time/text) → fingerprint matches, refresh is a complete no-op.

The only case that still produces a visible movement is the peer the user is *directly reading* changing the line they're anchored to — and that's unavoidable, you can't anchor to content that's being rewritten under your nose.

### Helper functions extracted to module level

- `_peer_fingerprint(p)` — the dirty-check tuple.
- `_format_peer_section(p)` — builds `(header_line, body, head_tag, body_tag)`. Previously inline in the loop; now shared between the surgical path and `_full_rebuild_peers_view` to keep the rendering single-sourced.

### Tests

`pytest -q`: **37/37 passing**. The renderer isn't covered by tests (it's UI/event-loop code), so the test count is just confirming nothing else regressed; `py_compile` clean.

### Files touched
- `netnotepad/renderer/tk_renderer.py` — refactored `refresh_peers_view`, added `_full_rebuild_peers_view`, extracted `_peer_fingerprint` and `_format_peer_section` to module level, added per-peer state dicts. Final file ~440 lines (was ~280).

## 2026-05-16 (debugging) - peer-section boundary fix

Three-machine LAN test surfaced a real bug in the surgical-update refactor above: with two other peers visible, the bottom pane started cycling between them — peer A's section visible for ~1 second, then peer B's, never both together.

### Diagnosis

The bug was at the boundary between consecutive peers' bracket marks.

Old peer-section layout (per `_format_peer_section`): header + body where body ended with `"\n\n"`. After inserting peer A's full section, `end_mark_A` was set at `"end-1c"`. The loop then moved to peer B, set `start_mark_B` at `"end-1c"` again — **the same buffer position** as `end_mark_A`.

End and start marks coinciding on a single buffer index is the failure mode. Walk through what happened on a surgical update for peer A:

1. `delete(start_mark_A, end_mark_A)` — peer A's content removed; `start_mark_A` and `end_mark_A` collapse to the same position. `start_mark_B` was already at that position, so all three coincide.
2. `insert(start_mark_A, new_header_A)` — content inserted at that position. Tk applies gravity:
   - `start_mark_A` has **left** gravity → stays on the left, new content goes right. ✓
   - `end_mark_A` has **right** gravity → moves to the right of the new content. ✓
   - `start_mark_B` has **left** gravity → stays on the left, new content goes right. **❌**
3. After the insert, `start_mark_B` is now sitting *before* peer A's new content. Peer B's bracket (`start_mark_B`..`end_mark_B`) now visually envelops peer A's new section as well as peer B's original content.
4. Next time peer B's fingerprint changes, `delete(start_mark_B, end_mark_B)` wipes peer A's section as collateral. Peer A vanishes; only peer B is visible.
5. Then peer A types again and the bracket dance flips the other way. Result: alternating display.

### Fix

Insert a `"\n"` separator **between** peer sections, **outside** the brackets, so `end_mark_X` and `start_mark_(X+1)` are at distinct buffer positions. Now an insert at `end_mark_X`'s position only affects `end_mark_X` (the only mark at that position), and `start_mark_(X+1)` is one character further along and uninvolved.

Two changes to `tk_renderer.py`:

- `_format_peer_section` now returns body terminated with a single `"\n"` (was `"\n\n"`). The blank-line visual separator between peers is no longer carried by the body — it's added explicitly.
- In `_full_rebuild_peers_view`'s per-peer loop, after setting `end_mark` with right gravity, do `peers_view.insert("end", "\n")` — this is the inter-section separator. It lives outside the brackets so it survives surgical updates untouched (every redraw is bounded by `start_mark`..`end_mark` which doesn't include the separator).

The surgical path itself didn't need any change — once the marks are at distinct positions, the existing `delete(start, end)` + `insert(start, header)` + `insert(end, body)` sequence works as intended.

### Visual layout (unchanged for the user)

Each peer's section still shows as: a header line, the body content (one or more lines), then a blank line before the next peer's header. The blank line is just sourced from a different place in the code now.

### Validation

- `pytest -q`: 37/37 passing (renderer is not under test, so this just confirms nothing else regressed).
- `py_compile` clean.
- Live two-/three-machine LAN re-test pending — couldn't be exercised in the Cowork sandbox because the Linux container has no Tkinter and no display server.

### Files touched
- `netnotepad/renderer/tk_renderer.py` — two small targeted edits: body terminator in `_format_peer_section`, separator insert in `_full_rebuild_peers_view`'s peer loop.

## 2026-05-16 (still debugging) - drop end_mark; track content length

The boundary-separator fix above (v1) didn't fully resolve the cycling — three-machine LAN re-test showed it now cycled "differently but still cycles." Diagnosis on the second look:

### Why v1 wasn't enough

The previous design kept a right-gravity `end_mark` per peer, set at `"end-1c"` after inserting the peer's header + body. v1 then did `peers_view.insert("end", "\n")` to drop a separator outside the section brackets.

The problem: at the moment of that separator insert, `end_mark` was *at the same buffer position* as the insert target. Tk normalizes `insert("end", ...)` to "insert just before the trailing newline" — which is exactly where `end-1c` is. With right gravity, `end_mark` got dragged forward past the separator, landing on the new `end-1c`. The next peer's `start_mark` was then set at `end-1c` and ended up at the SAME position as the previous peer's `end_mark`. Coinciding mark indices is the bug we were trying to avoid — separator or no separator, the gravity drift put them back in conflict.

### v2 fix: kill end_mark

Replace `(start_mark, end_mark)` with `(start_mark, content_len)`. Only one mark per peer (left gravity, anchored to the start of the section). End-of-section is computed on demand as `peers_view.index(f"{start_mark} + {content_len}c")` — a derived index, not a mark. Derived indices can't drift because they're recomputed on each operation.

`peer_section_marks` is now `dict[str, tuple[str, int]]`. Surgical update becomes:

1. `end_idx = peers_view.index(f"{start_mark} + {old_content_len}c")` — derived position of the section's current end.
2. `delete(start_mark, end_idx)` — wipe the section.
3. `insert(start_mark, header, head_tag)` — start_mark stays put (left gravity), header goes to its right.
4. `insert(f"{start_mark} + {len(header)}c", body, body_tag)` — body lands immediately after the freshly-inserted header, before the separator that lives just past it.
5. Update `peer_section_marks[hostname] = (start_mark, len(header) + len(body))`.

With only one mark per peer, the boundary between sections can't be made worse by mark drift. The separator is still inserted after each section (preserving the visual blank line); it's now structurally important because without it the next peer's `start_mark` and the current peer's start_mark + content_len index would coincide, and we'd be back to having two marks at the same index.

### Tests

- `pytest -q`: 37/37 passing.
- `py_compile` clean.
- Live three-machine LAN re-test pending — that's the real verification.

### Files touched
- `netnotepad/renderer/tk_renderer.py` — middle of the file rewritten to swap `(start_mark, end_mark)` for `(start_mark, content_len)`, with the surgical loop and `_full_rebuild_peers_view`'s anchor capture / mark unset both updated to match. Module-level docstring updated to describe the single-mark scheme.

## 2026-05-16 (postscript) - false alarm + defensive logging

Three-machine LAN re-test came back: "both clients show but one client's text is missing." Initially suspected the surgical-update refactor again — added a defensive try/except around the surgical insert loop with a full-rebuild fallback on any `tk.TclError`, plus `log_exception` with context `tk.surgical_update[<hostname>]` so any failure would land in `~/.netnotepad/log.txt` with the offending peer.

Matthew's log showed zero `tk.surgical_update` entries — just network-level `mesh.connect` timeouts to one specific peer (the desktop, IP 10.0.0.2). Followup conversation confirmed: the desktop's network card had been temporarily disabled. So zeroconf still announced the peer (mDNS UDP coming through some path; possibly cached or via a different interface), the renderer created its peer entry, but the TCP mesh never completed a handshake → no `Snapshot` exchanged → `block_text` stayed `""`. The renderer faithfully substituted the `"(empty)"` placeholder from `_format_peer_section`'s `(p.block_text or "(empty)") + "\n"` rule. Visually identical to "text missing."

No renderer bug. The single-mark / content-len rewrite is correct. The surgical update path stays in place.

### Defensive logging kept

The try/except around the surgical insert (with the full-rebuild fallback if it ever fires) is staying in. Cheap insurance: if Tk ever does throw mid-section in some future scenario, we don't leave a torn render on screen, and the offending peer ends up in the log under a known context tag.

### Open UX nit (not done)

The `"(empty)"` placeholder is currently shown for two different states: "peer has an empty block" and "we've never received any block_text from this peer." Distinguishing them would mean adding `has_received_snapshot: bool` to the Peer dataclass and rendering `"(no content received yet)"` vs `"(empty)"` accordingly. Worth doing eventually so this kind of debugging doesn't take a detour through "is the renderer broken?" — but not urgent.

### Files touched
- `netnotepad/renderer/tk_renderer.py` — wrapped the per-peer surgical update in `try/except tk.TclError`, with `log_exception(..., context=f"tk.surgical_update[{p.hostname}]")` and a `surgical_failed` flag that triggers `_full_rebuild_peers_view(peers)` after the loop. Added a `break` out of the surgical loop on first failure so we don't keep stacking corruptions.
