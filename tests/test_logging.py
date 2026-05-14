"""Tests for the diagnostic logging shim.

Covers:
  * log_exception writes a describe_error report into log.txt, with context.
  * log_info writes an informational line into log.txt.
  * crash_to_file writes a for_claude() report into last_crash.txt.
  * a deliberate raise inside a background thread, routed through
    log_exception, ends up in log.txt.
  * the log calls never raise even if the log dir is unwritable.

The logger uses module-level state (_LOG_DIR), so we explicitly reset it
inside a fixture via set_log_dir(tmp_path) for each test, and restore
afterwards.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from netnotepad import log as log_module
from netnotepad.log import (
    crash_to_file,
    get_log_dir,
    log_exception,
    log_info,
    set_log_dir,
)


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    """Redirect the logger to a temp dir for each test, then restore."""
    prev = log_module._LOG_DIR
    set_log_dir(tmp_path)
    try:
        yield tmp_path
    finally:
        log_module._LOG_DIR = prev


def _read_log(log_dir: Path) -> str:
    p = log_dir / "log.txt"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def test_log_info_writes_line(log_dir: Path) -> None:
    log_info("hello world", context="test.info")
    contents = _read_log(log_dir)
    assert "hello world" in contents
    assert "[test.info]" in contents
    assert "[INFO" in contents


def test_log_exception_writes_traceback(log_dir: Path) -> None:
    try:
        int("not a number")
    except ValueError as e:
        log_exception(e, context="test.exc")
    contents = _read_log(log_dir)
    assert "[test.exc]" in contents
    assert "ValueError" in contents
    assert "invalid literal" in contents
    # describe_error's concise format includes a Traceback header
    assert "Traceback (most recent call last)" in contents


def test_log_exception_with_no_context_still_works(log_dir: Path) -> None:
    try:
        {}["nope"]
    except KeyError as e:
        log_exception(e)
    contents = _read_log(log_dir)
    assert "KeyError" in contents


def test_crash_to_file_writes_for_claude_report(log_dir: Path) -> None:
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as e:
        path = crash_to_file(e)
    assert path is not None
    assert path == log_dir / "last_crash.txt"
    body = path.read_text(encoding="utf-8")
    # for_claude() output has labeled sections
    assert "=== ERROR REPORT (heavy edition) ===" in body
    assert "PRIMARY EXCEPTION" in body
    assert "RuntimeError" in body
    assert "kaboom" in body


def test_log_exception_from_background_thread(log_dir: Path) -> None:
    """A deliberate raise inside a worker thread, routed via log_exception,
    must show up in log.txt - this is the actual usage pattern in the mesh."""
    started = threading.Event()
    done = threading.Event()

    def worker() -> None:
        started.set()
        try:
            raise OSError(99, "simulated mesh hiccup", "/some/path")
        except OSError as e:
            log_exception(e, context="test.worker")
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert started.wait(timeout=2.0)
    assert done.wait(timeout=2.0)
    t.join(timeout=2.0)

    contents = _read_log(log_dir)
    assert "[test.worker]" in contents
    assert "OSError" in contents
    assert "simulated mesh hiccup" in contents
    # OSError dispatch extractor surfaces errno/filename
    assert "errno=99" in contents or "errno: 99" in contents
    assert "/some/path" in contents


def test_concurrent_log_writes_dont_corrupt(log_dir: Path) -> None:
    """The module-level lock should serialize writes from multiple threads."""
    N = 20

    def worker(i: int) -> None:
        try:
            raise ValueError("msg-" + str(i))
        except ValueError as e:
            log_exception(e, context="test.concurrent." + str(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    contents = _read_log(log_dir)
    for i in range(N):
        assert "msg-" + str(i) in contents, "missing msg-" + str(i)


def test_log_exception_does_not_raise_when_dir_is_unwritable(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If mkdir/open fails, log_exception should swallow the OSError silently."""
    def boom(*args, **kwargs):  # pragma: no cover - argument shape varies
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", boom)
    # mkdir is fine; the open() inside _append is what we want to break.
    try:
        raise RuntimeError("won't get logged but mustn't crash either")
    except RuntimeError as e:
        # Should NOT raise, even though the underlying write blows up.
        log_exception(e, context="test.unwritable")


def test_set_log_dir_changes_destination(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    prev = log_module._LOG_DIR
    try:
        set_log_dir(a)
        log_info("first")
        set_log_dir(b)
        log_info("second")
        assert (a / "log.txt").exists()
        assert (b / "log.txt").exists()
        assert "first" in (a / "log.txt").read_text(encoding="utf-8")
        assert "second" in (b / "log.txt").read_text(encoding="utf-8")
        # No bleed
        assert "second" not in (a / "log.txt").read_text(encoding="utf-8")
        assert "first" not in (b / "log.txt").read_text(encoding="utf-8")
    finally:
        log_module._LOG_DIR = prev


def test_get_log_dir_defaults_when_unset() -> None:
    """When set_log_dir has never been called, get_log_dir falls back to
    ``~/.netnotepad`` rather than raising."""
    prev = log_module._LOG_DIR
    log_module._LOG_DIR = None
    try:
        d = get_log_dir()
        assert isinstance(d, Path)
        assert d.name == ".netnotepad"
    finally:
        log_module._LOG_DIR = prev
