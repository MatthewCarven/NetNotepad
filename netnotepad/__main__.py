"""Entry point: ``python -m netnotepad``.

Wraps the renderer in a top-level try/except that:
  1. Logs the exception to ``~/.netnotepad/log.txt`` via ``log_exception``.
  2. Writes a heavy/for_claude() report to ``~/.netnotepad/last_crash.txt``
     so the user can paste it straight into a Claude chat for diagnosis.
  3. Prints the crash report path to stderr and exits non-zero.

KeyboardInterrupt is treated as a clean exit, not a crash.
"""

from __future__ import annotations

import argparse
import sys

from netnotepad.engine import NetNotepad
from netnotepad.log import crash_to_file, log_exception, log_info, set_log_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="netnotepad",
        description="netnotepad — a tiny LAN scratchpad shared with peers on your network.",
    )
    parser.add_argument(
        "--renderer",
        choices=["tk", "term"],
        default="tk",
        help="UI renderer (default: tk)",
    )
    args = parser.parse_args()

    engine = NetNotepad()
    # Point the logger at the same data dir the engine uses for mine.txt.
    set_log_dir(engine.data_dir)
    log_info("netnotepad starting", context="main")

    try:
        if args.renderer == "tk":
            from netnotepad.renderer.tk_renderer import run as run_tk

            run_tk(engine)
        else:
            from netnotepad.renderer.term_renderer import run as run_term

            run_term(engine)
    except KeyboardInterrupt:
        log_info("netnotepad shutting down (keyboard interrupt)", context="main")
    finally:
        try:
            engine.shutdown()
        except Exception as e:
            log_exception(e, context="main.shutdown")
    return 0


def _entrypoint() -> int:
    """Outer wrapper that catches anything main() didn't, so the user
    always gets a crash file rather than a bare Python traceback."""
    try:
        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 0
    except BaseException as e:
        try:
            log_exception(e, context="main.unhandled")
            path = crash_to_file(e)
        except BaseException:
            path = None
        try:
            print(
                "\nnetnotepad crashed: " + type(e).__name__ + ": " + str(e),
                file=sys.stderr,
            )
            if path is not None:
                print("crash report: " + str(path), file=sys.stderr)
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_entrypoint())
