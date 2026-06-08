# Terminal renderer — implementation plan

**Status: plan only (written 2026-06-08).** No renderer code has landed yet.
This note is the design to build from next session. The `term` branch in
`__main__.py` is still the "not implemented yet" stub.

## Why this renderer exists

Two renderers, one engine. The Tk renderer drives editing *natively* — the Tk
Text widget owns selection / paste / IME / undo, and we mirror the result into
the engine via `set_local_text`, which emits a **Snapshot** on every change.
That means the whole **Delta** path (`engine.insert` / `delete_backward` /
`delete_forward` / `move_cursor`, each emitting an incremental `Delta`) is never
exercised by a human-driven renderer — only by tests.

The terminal renderer's job is to be the renderer that drives editing through
the engine's own cursor + edit primitives, so the `Delta` wire path gets daily
real-world use. The engine groundwork is already in place and tested: `Document`
emits Deltas from those methods, and `_apply_delta_to_peer` applies incoming
Deltas grapheme-by-grapheme (both green as of the 50/50 and 75/75 suites). This
renderer is the last consumer needed to close that loop.

## Dependency

New third-party dependency: **prompt_toolkit** (plan verified against **3.0.52**).
Add to:

- README install line (`pip install regex zeroconf prompt_toolkit`).
- `build_exe.bat` PyInstaller flags — likely needs `--collect-all prompt_toolkit`
  the same way zeroconf does, since prompt_toolkit imports some submodules
  dynamically. Confirm the `.exe` launches the term renderer after building.

prompt_toolkit owns the screen, the input parser, and efficient full-screen
diffing — so this renderer does **not** need the hand-rolled surgical-diff
machinery the Tk peers pane carries. We re-render from engine state on each
invalidate and let the framework compute the minimal screen update.

## Module + contract

`netnotepad/renderer/term_renderer.py`, exposing `run(engine) -> None`, mirroring
`tk_renderer.run`. The `else` branch in `__main__.main()` (currently the stub)
becomes:

```python
from netnotepad.renderer.term_renderer import run as run_term
run_term(engine)
```

The existing outer try/except/finally in `main()` stays as-is (KeyboardInterrupt
= clean exit; `finally` calls `engine.shutdown()`).

## Layout (full-screen `Application`)

Top to bottom, an `HSplit`:

1. **Status bar** — one line: `hostname · N peer(s) (M offline) · <transient>`.
   Same content as Tk's `refresh_status`. A `FormattedTextControl` whose callable
   reads `engine.hostname` + `engine.peers` live.
2. **Own block** — the editable region. Header `hostname (you)`, then the
   editable text.
3. **Separator** — a one-line horizontal rule.
4. **Peers pane** — read-only, scrollable. One section per `engine.sorted_peers()`:
   a header (`hostname · address · edited HH:MM · offline`) and a body
   (`block_text`, or `(empty)`, or `(no content received yet)` keyed off
   `has_received_snapshot`), dimmed when `tombstoned`. Reuses the exact body-state
   logic from `_format_peer_section`.

## The editing model (the crux)

We deliberately do **not** use a prompt_toolkit `Buffer` / `TextArea` for the own
block — that would own its own editing and put us right back in the
mirror-the-widget posture that defeats the purpose. Instead:

- The own block is a **`FormattedTextControl`** whose text callable returns
  `engine.document.text` (styled, including attachment-token highlighting), and
  whose **`get_cursor_position`** returns `Point(x=col, y=line)` from
  `engine.document.cursor`. The `Window` places the real terminal cursor there
  and handles vertical scrolling to keep it visible. (Both APIs verified present
  in 3.0.52.)
- A `KeyBindings` set translates every editing key into an **engine** call, then
  calls `app.invalidate()` to redraw:

| Key | Engine call |
| --- | --- |
| any printable char | `engine.insert(char)` |
| Enter | `engine.insert("\n")` |
| Backspace | `engine.delete_backward()` |
| Delete | `engine.delete_forward()` |
| Left / Right | `engine.move_cursor(...)` — computed target (see below) |
| Up / Down | `engine.move_cursor(line ± 1, col)` (engine clamps col) |
| Home / End | `engine.move_cursor(line, 0)` / `(line, big)` (clamps to line end) |
| Tab | `engine.insert("    ")` — 4 spaces (decision; literal `\t` is the alternative) |
| Ctrl-S | `engine.save()` |
| Ctrl-Q / Ctrl-C | save → shutdown → exit app |

Cursor arithmetic for Left/Right (the engine only exposes absolute
`move_cursor(line, col)`, no relative move): a small renderer-local helper splits
`engine.document.text` on `\n` and counts **graphemes** per line (reusing the
engine's `_graphemes`). Left at col>0 → `(line, col−1)`; at col 0 with line>0 →
`(line−1, len(prev line))`. Right is the mirror. Keeps motion grapheme-aware and
identical to the engine's own indexing.

Because every edit goes through `engine.insert/delete_*`, each keystroke emits a
`Delta` that the mesh broadcasts — the entire point of the exercise.

## Threading model

- prompt_toolkit runs its asyncio event loop on the **main thread**. Key handlers
  run there, so calling `engine.insert(...)` + `app.invalidate()` from them is
  safe and ordered.
- `engine.start_networking()` blocks ~1.5s (zeroconf probe). Run it on a **daemon
  thread**, exactly like Tk's `start_net`. On failure, `log_exception(context=
  "term.start_networking")` and surface a transient status.
- Peer callbacks (`on_peer_changed`, `on_peer_tombstoned`) fire on zeroconf/mesh
  **background threads**. They must not touch the UI directly. Pattern: each
  callback just calls `app.invalidate()` (documented thread-safe — it schedules
  the redraw on the loop). The status bar and peers pane read engine state at
  render time, so nothing needs marshalling across the thread boundary.
- Read-safety: `engine.sorted_peers()` takes the engine's `_peers_lock`.
  `engine.document` is mutated only from the main thread (our key handlers). A
  peer's `block_text` is mutated on mesh threads, but a stale read just yields a
  one-frame-old display, corrected on the next invalidate — the same lax-but-fine
  posture the Tk renderer already takes. Don't over-engineer locking for a
  scratchpad.

## Save + exit

- **Ctrl-S** → explicit `engine.save()` + transient "saved".
- **Autosave (optional for v1)** — a periodic loop-timer
  (`get_event_loop().call_later`, re-armed) that saves every few seconds if the
  document changed since last save. Cheap insurance against a hard kill. Tk
  debounces 500ms off `<<Modified>>`; the terminal equivalent is a small idle
  timer. v1 may ship with Ctrl-S + exit-save only and add the timer if it feels
  needed.
- **Exit** (Ctrl-Q / Ctrl-C): `engine.save()` then `engine.shutdown()`, each
  wrapped in try/except + `log_exception` (`term.save`, `term.shutdown`), then
  `app.exit()`.

## Attachments — deferred within this renderer

The Tk renderer has an Attach button, token highlighting in both panes, and a
right-click "Save attachment as…". For the terminal renderer's **first** version,
scope is the text-editing-via-Delta path + peer display. Attachment **tokens
render as styled text** in both panes (reuse `parse_attachment_tokens`, give them
a distinct color) so they're visible, but the attach / save-as UI (a path-input
dialog to attach, save-under-cursor to fetch) is a **follow-up** once the core
renderer is proven. Track it in TODO under the renderer.

## Test approach (headless — the sandbox has no TTY)

prompt_toolkit supports headless testing; verified in 3.0.52 that
`create_pipe_input()` is a context manager and `DummyOutput` exists.

1. **Pure helpers** (no app): cursor arithmetic (left / right / up / down / home /
   end target computation), per-line grapheme-length helper, status-string
   formatting, peer-section formatting. Fast, deterministic.
2. **Key→Delta integration**: build the `Application` with `create_pipe_input()`
   + `DummyOutput`, attach a recorder to `engine.on_local_change`, feed a scripted
   byte sequence (e.g. `"hello"`, then arrow-left escapes, then more text), run
   the app briefly, and assert both (a) `engine.document.text` is correct and
   (b) the recorded `Delta` sequence matches expectation. This verifies the whole
   Delta path end-to-end **without a terminal** — the highest-value test here.
3. Keep the existing **75** tests green.

New file `tests/test_term_renderer.py`, ~12–18 tests. Verification:
`PYTHONPYCACHEPREFIX=/tmp/nnpy python -B -m pytest -q` and `py_compile`. If
Write/Edit desync with the bash mount, rewrite the module via a `cat << 'EOF'`
heredoc and verify with `wc -l` (the documented mount-drift workaround).

## Known rough edges (acceptable for v1 — note, don't fix yet)

- **Wide characters** — our cursor `col` is in graphemes; `get_cursor_position`'s
  `x` is in terminal **cells**. On a line with wide emoji the cursor may sit a
  cell off. Fine for a scratchpad; revisit with a wcwidth-aware column→cell map if
  it grates.
- **Remembered/virtual column** on Up/Down (preserving your original column when
  passing through a short line) is **not** in v1 — we pass the current col and let
  `move_cursor` clamp. Deferred.
- **PageUp/PageDown to scroll the peers pane** when many peers are present —
  optional for v1.

## Estimated size

`term_renderer.py` ~250–350 lines; `tests/test_term_renderer.py` ~200 lines. One
focused session.
