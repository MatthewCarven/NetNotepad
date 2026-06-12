# Network Notepad

A LAN-shared scratchpad. Every peer on your local network gets their own editable block; everyone sees everyone else's blocks update in near-real-time as they type. No server, no account, no cloud — just zeroconf discovery and a TCP mesh between peers.

The itch: you have more than one computer on the same network and you want a frictionless way to move a snippet of text — a URL, a path, a paragraph, a command line — from one to another. Cloud sync is overkill. Copy-pasting through a chat app is silly. USB sticks are stone-age. Open Network Notepad on both, start typing, done.

## Features

- **Zeroconf / mDNS discovery.** Peers find each other automatically on the LAN. No IP addresses to configure, no central server to point at.
- **Region-per-peer model.** Each peer owns exactly one block. Nobody's editing the same bytes at the same time, so there's no need for OT, CRDTs, or anything else that makes collaborative editing famously hard.
- **Cross-platform.** Pure Python plus Tkinter (which ships with Python). Tested on Windows. Should work anywhere Python and Tk run.
- **Grapheme-aware cursor.** An emoji with a skin-tone modifier is one cursor position, not five code points. (Uses the third-party `regex` library's `\X` pattern.)
- **File attachments.** Attach a file and a token appears inline in your block; peers fetch the bytes on demand over a content-addressed blob store (right-click a token to save it). Up to 50 MB per file. Image attachments show as inline thumbnails in the Tk UI (png/gif out of the box; `pip install pillow` for jpeg/webp and smoother scaling — optional).
- **Local persistence.** Your own block is saved to `~/.netnotepad/mine.txt` and reloaded on next launch.
- **Graceful disconnect handling.** Peer goes offline → their last-known block stays visible, dimmed, marked offline. Peer comes back → it lights up and resumes syncing. Brief network blips (sub-5 seconds) don't even cause a UI flicker.
- **Diagnostic logging built in.** Every silent failure in the background mesh writes to `~/.netnotepad/log.txt`. Unhandled crashes write a full report to `~/.netnotepad/last_crash.txt` that you can paste straight into a Claude chat for diagnosis.

<img width="762" height="672" alt="image" src="https://github.com/user-attachments/assets/35615ad0-68ba-4d97-80e9-b9b4af465241" />

Yeah about that screenshot, like you never need it right now but when you do......

## Windows Binary

https://drive.google.com/file/d/1hko5shsAkPfTts0J9A3PPt7-4QTCRsaR/view?usp=sharing

## Install and run

Requires Python 3.10 or newer.

```
pip install regex zeroconf prompt_toolkit
python -m netnotepad
```

That's it. On Windows there's a `start.bat` that does the `pip install` and the `python -m netnotepad` for you — double-click it.

Run the same thing on a second machine on the same LAN and within a couple of seconds each will appear in the other's peers pane. Start typing in your editable pane and the text shows up in the other peer's read-only view of your block almost immediately.

## How it works (briefly)

There are two layers, and you can substitute renderers without touching the network code:

- **Engine** (`netnotepad/engine/`) — the headless core. Owns the local document, the peer state, zeroconf discovery, and the TCP mesh. Talks to renderers through callback lists (`on_local_change`, `on_peer_changed`, `on_peer_tombstoned`).
- **Renderer** (`netnotepad/renderer/`) — `tk_renderer.py` (Tkinter GUI, the default) and `term_renderer.py` (`prompt_toolkit` full-screen terminal UI — run with `python -m netnotepad --renderer term`).

The mesh uses a lex-ordered "only one side initiates" rule so each pair of peers ends up with exactly one TCP socket between them, no double-connect races, no coordinator needed. Heartbeats every 5 seconds; a watchdog closes any connection that's been silent for 25 seconds, which then triggers an automatic reconnect.

For more depth see [`DESIGN.md`](DESIGN.md) (architecture rationale) and [`WORKLOG.md`](WORKLOG.md) (chronological account of what was built and why).

## Project layout

```
netnotepad/
  __main__.py            entry point — `python -m netnotepad`
  protocol.py            wire format (line-delimited JSON, six message types)
  error_handler.py       describe_error() — surfaces everything knowable
                         about any exception; never raises
  log.py                 log_info / log_exception / crash_to_file shim
  engine/
    __init__.py          NetNotepad facade — what renderers talk to
    document.py          local block model + grapheme cursor + persistence
    discovery.py         zeroconf register + browse
    network.py           TCP mesh + heartbeat + watchdog + auto-reconnect
    attachments.py       content-addressed blob store + HTTP blob server/fetch
    fileio.py            File-menu open/export helpers (display-free)
  renderer/
    tk_renderer.py       Tkinter UI
    preview.py           image-preview helpers (display-free)
    term_renderer.py     prompt_toolkit terminal UI (--renderer term)

tests/                   pytest suite, 136 tests
DESIGN.md                why things are the way they are
WORKLOG.md               what happened, day by day
TODO.md                  what's next, what's later, what's verified
```

## Building a standalone Windows .exe
Yeah this is kind of what you really need straight up so here is a prebuilt .exe to play with https://drive.google.com/file/d/11HWSPNTZeq505zmPGDb993FpBPfQQ6A_/view?usp=sharing

Double-click `build_exe.bat`. It installs PyInstaller if you don't have it, then produces `dist\netnotepad.exe` — a single self-contained binary (about 15-25MB) you can drop anywhere on your PATH. After that, `netnotepad` from any command prompt or Win+R Run dialog just works.

Don't drop it in `C:\Windows`; make a `%USERPROFILE%\bin` directory and add THAT to your user PATH once. Future tools you build go in the same folder.

## Status

Working and in daily use across Matthew's LAN (laptop, desktop, and a slow box affectionately known as Meatthread0). Things that are solid:

- Auto-discovery across the LAN.
- Bidirectional keystroke-rate sync over TCP.
- Reconnect after network blips, reboots, and antivirus tantrums.
- Graceful offline display with a 5s grace window to avoid flicker.
- Crash reports written to disk for any unhandled exception.
- File → Open / Save As in both UIs (Ctrl-O / Ctrl-E in the terminal): open a text file into your block, export your block or any peer's last-known block to a file. Autosave still handles the everyday persistence.

Things that are planned and not yet built (see [`TODO.md`](TODO.md) for the full list):

- Multi-block per peer (named panes within one peer — "scratch", "URLs", etc.).
- Cross-subnet / VPN-friendly discovery.

## Tests

```
pip install pytest regex zeroconf prompt_toolkit
python -m pytest tests/ -q
```

136 tests covering the document model, discovery, the TCP mesh end-to-end (two real engines mutually discovering and syncing), the diagnostic logging, and the tombstone grace period.

## License

See [`LICENSE.md`](LICENSE.md).

## Acknowledgements

Built collaboratively with Claude (Anthropic) in Cowork mode. The `error_handler.py` module — `describe_error()` with its `for_claude()` LLM-friendly output — was written in a separate Cowork session and vendored in here; if it's useful to your project too, the file is dependency-free and drops into any Python codebase.
