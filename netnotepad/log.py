"""
Diagnostic logging for netnotepad.

Two outputs, both under the engine's data dir (``~/.netnotepad`` by default):

  log.txt          - rolling event log. Every caught exception in a
                     background thread, every informational milestone, gets
                     appended here via ``describe_error().to_string()``.
                     This is the file to read for post-mortem debugging.
  last_crash.txt   - last unhandled crash from ``__main__``. Written with
                     ``describe_error(e, include_locals=True).for_claude()``
                     so the user can paste it straight into a Claude chat.

Design contract: nothing in this module raises. If the file system itself
is hostile (perms, disk full, weird dir), the log call silently no-ops
rather than escalating into a second exception on top of the first.

Usage:

    from netnotepad.log import log_exception, log_info, crash_to_file, set_log_dir

    # once, at engine construction:
    set_log_dir(engine.data_dir)

    # anywhere in a background thread:
    try:
        risky()
    except Exception as e:
        log_exception(e, context="mesh.reader")

    # informational milestones:
    log_info("falling back to kernel-assigned port", context="mesh.bind")

    # top-level crash from __main__:
    crash_to_file(unhandled)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from netnotepad.error_handler import describe_error


_DEFAULT_LOG_DIR = Path.home() / ".netnotepad"
_LOCK = threading.Lock()
_LOG_DIR: Optional[Path] = None

# Cap log.txt at this size; on overflow it rolls to log.1.txt and a fresh
# log.txt starts. One backup only — this is a breadcrumb trail for
# post-mortem debugging, not an audit log, and the most useful entries are
# always the most recent ones. Exposed as a module attribute so tests can
# drop it to a tiny value without writing a megabyte.
LOG_MAX_BYTES = 1_000_000


def set_log_dir(path: Path) -> None:
    """Point the logger at a specific directory. Idempotent."""
    global _LOG_DIR
    _LOG_DIR = Path(path)


def get_log_dir() -> Path:
    """Return the configured log dir, defaulting to ``~/.netnotepad``."""
    return _LOG_DIR if _LOG_DIR is not None else _DEFAULT_LOG_DIR


def _ensure_dir() -> Optional[Path]:
    dirp = get_log_dir()
    try:
        dirp.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return dirp


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _maybe_rotate(log_path: Path) -> None:
    """If ``log_path`` is at or past ``LOG_MAX_BYTES``, roll it to log.1.txt.

    Caller must hold ``_LOCK``. Single backup file — any prior log.1.txt is
    overwritten. Never raises: a hostile filesystem during rotation (perms,
    race with antivirus, etc.) just leaves the existing log in place and
    the next append will fail-soft like every other path here.
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return
    if size < LOG_MAX_BYTES:
        return
    backup_path = log_path.with_name("log.1.txt")
    try:
        if backup_path.exists():
            backup_path.unlink()
    except OSError:
        pass
    try:
        log_path.rename(backup_path)
    except OSError:
        pass


def _append(line: str) -> None:
    """Append a line to log.txt. Never raises. Rotates to log.1.txt if the
    current log is at or past ``LOG_MAX_BYTES``."""
    dirp = _ensure_dir()
    if dirp is None:
        return
    try:
        with _LOCK:
            log_path = dirp / "log.txt"
            _maybe_rotate(log_path)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
    except OSError:
        pass


def log_info(msg: str, *, context: str = "") -> None:
    """Append an informational message to log.txt."""
    ctx = "[" + context + "] " if context else ""
    _append("[" + _ts() + "] [INFO ] " + ctx + str(msg))


def log_exception(exc: BaseException, *, context: str = "") -> None:
    """Append a description of an exception to log.txt. Never raises.

    The describe_error report is a multi-line traceback-style block; we
    indent each line by two spaces under a single header so the entries
    are easy to scan even when several pile up close together.
    """
    try:
        body = describe_error(exc).to_string()
    except BaseException:
        try:
            body = "<describe_error failed; repr: " + repr(exc) + ">"
        except BaseException:
            body = "<describe_error failed; repr unavailable>"
    ctx = "[" + context + "] " if context else ""
    header = "[" + _ts() + "] [ERROR] " + ctx
    indented = "\n".join("  " + ln for ln in body.splitlines()) or "  <empty>"
    _append(header + "\n" + indented)


def crash_to_file(exc: BaseException) -> Optional[Path]:
    """Write the heavy/for_claude() form of ``exc`` to last_crash.txt.

    Returns the path written, or None if the write failed. Never raises.
    Includes frame locals because this is the deliberate paste-into-Claude
    crash report - we want all the context we can get.
    """
    dirp = _ensure_dir()
    if dirp is None:
        return None
    try:
        body = describe_error(exc, include_locals=True).for_claude()
    except BaseException:
        try:
            body = "describe_error failed; repr: " + repr(exc)
        except BaseException:
            body = "describe_error failed and repr unavailable"
    path = dirp / "last_crash.txt"
    try:
        with _LOCK:
            with open(path, "w", encoding="utf-8") as f:
                f.write("netnotepad crash report - " + _ts() + "\n\n")
                f.write(body)
                if not body.endswith("\n"):
                    f.write("\n")
        return path
    except OSError:
        return None


__all__ = [
    "set_log_dir",
    "get_log_dir",
    "log_info",
    "log_exception",
    "crash_to_file",
]
