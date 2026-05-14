"""
Local document model.

Holds the user's own block of text, the cursor, and disk persistence. Knows
nothing about networking - it just emits ``Delta`` or ``Snapshot`` operations
through a callback when the local state changes; the engine is responsible
for broadcasting those.

Text positions are tracked in **grapheme clusters**. That's the right unit
for a cursor: an emoji with a skin-tone modifier is one position even
though it's several code points and many bytes. We use the third-party
``regex`` library's ``\X`` pattern when available, with a code-point
fallback when it isn't.

This class is not thread-safe; the engine is expected to hold a lock if it
needs to call in from a non-owning thread. Tkinter's main loop will be the
primary caller and that's single-threaded by default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import regex as _re  # third-party `regex`, supports \X grapheme clusters

    _GRAPHEME_RE = _re.compile(r"\X")

    def _graphemes(s: str) -> list[str]:
        return _GRAPHEME_RE.findall(s)

except ImportError:  # pragma: no cover - fallback path
    def _graphemes(s: str) -> list[str]:
        return list(s)


@dataclass
class Cursor:
    """Cursor position within our own block. 0-indexed, in graphemes."""

    line: int = 0
    col: int = 0


class Document:
    """
    Our own editable block of the shared notepad.

    Internally stored as a list of grapheme clusters so position arithmetic
    is O(1) per op and the cursor can move by user-visible characters.
    """

    def __init__(
        self,
        save_path: Optional[Path] = None,
        on_change: Optional[Callable[[object], None]] = None,
    ):
        self._graphemes: list[str] = []
        self.cursor = Cursor()
        self._seq = 0
        self._save_path = save_path
        self._on_change = on_change
        self._last_edit_ts: float = 0.0

        if save_path and save_path.exists():
            self._load()

    # ---------- public API ----------

    @property
    def text(self) -> str:
        return "".join(self._graphemes)

    @property
    def grapheme_count(self) -> int:
        return len(self._graphemes)

    @property
    def last_edit_ts(self) -> float:
        return self._last_edit_ts

    def insert(self, s: str) -> None:
        """Insert ``s`` at the cursor. ``s`` may contain newlines and multiple graphemes."""
        if not s:
            return
        pos = self._cursor_to_index()
        new_graphemes = _graphemes(s)
        self._graphemes[pos:pos] = new_graphemes
        self._set_cursor_from_index(pos + len(new_graphemes))
        self._emit_delta(pos=pos, remove=0, insert=s)

    def delete_backward(self, n: int = 1) -> None:
        """Backspace ``n`` graphemes before the cursor. No-op at start of block."""
        if n <= 0:
            return
        end = self._cursor_to_index()
        start = max(0, end - n)
        if start == end:
            return
        removed = end - start
        del self._graphemes[start:end]
        self._set_cursor_from_index(start)
        self._emit_delta(pos=start, remove=removed, insert="")

    def delete_forward(self, n: int = 1) -> None:
        """Forward-delete ``n`` graphemes at and after the cursor."""
        if n <= 0:
            return
        start = self._cursor_to_index()
        end = min(len(self._graphemes), start + n)
        if start == end:
            return
        removed = end - start
        del self._graphemes[start:end]
        self._set_cursor_from_index(start)
        self._emit_delta(pos=start, remove=removed, insert="")

    def move_cursor(self, line: int, col: int) -> None:
        """Move the cursor, clamping to a valid (line, col) for the current text."""
        text = self.text
        lines = text.split("\n") if text else [""]
        line = max(0, min(line, len(lines) - 1))
        line_graphemes = _graphemes(lines[line])
        col = max(0, min(col, len(line_graphemes)))
        self.cursor = Cursor(line=line, col=col)

    def cursor_to_nearest_valid(self) -> None:
        """Snap the cursor to the nearest valid position."""
        self.move_cursor(self.cursor.line, self.cursor.col)

    def set_text(self, new_text: str) -> None:
        """Replace the entire block with ``new_text`` and emit a Snapshot.

        Use this when an external editor (a Tk Text widget, a paste op, an
        undo) is the source of truth for the user's editing UX and the
        Document just needs to mirror the result. The cursor is clamped to
        a position still valid in the new text.

        No-ops when the text is unchanged - avoids spurious snapshot fanout
        from idempotent <<Modified>> events.
        """
        if new_text == self.text:
            return
        self._graphemes = _graphemes(new_text)
        self.cursor_to_nearest_valid()
        self._seq += 1
        self._last_edit_ts = time.time()
        if self._on_change is None:
            return
        from netnotepad.protocol import Snapshot
        self._on_change(Snapshot(content=new_text, seq=self._seq))

    # ---------- persistence ----------

    def save(self) -> None:
        """Write the block to disk atomically (temp file + rename)."""
        if not self._save_path:
            return
        self._save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._save_path.with_suffix(self._save_path.suffix + ".tmp")
        tmp.write_text(self.text, encoding="utf-8")
        tmp.replace(self._save_path)

    def _load(self) -> None:
        if not self._save_path or not self._save_path.exists():
            return
        try:
            content = self._save_path.read_text(encoding="utf-8")
        except OSError as e:
            # Saved block is unreadable (permissions, partial write during a
            # prior crash, etc.). Log and start with an empty block rather
            # than refusing to launch.
            from netnotepad.log import log_exception
            log_exception(
                e, context="document.load (" + str(self._save_path) + ")"
            )
            return
        self._graphemes = _graphemes(content)
        self._set_cursor_from_index(len(self._graphemes))

    # ---------- internals ----------

    def _emit_delta(self, *, pos: int, remove: int, insert: str) -> None:
        self._seq += 1
        self._last_edit_ts = time.time()
        if self._on_change is None:
            return
        from netnotepad.protocol import Delta
        self._on_change(Delta(pos=pos, remove=remove, insert=insert, seq=self._seq))

    def _cursor_to_index(self) -> int:
        """Translate the (line, col) cursor to a flat grapheme index."""
        if not self._graphemes:
            return 0
        idx = 0
        line_count = 0
        while idx < len(self._graphemes) and line_count < self.cursor.line:
            if self._graphemes[idx] == "\n":
                line_count += 1
            idx += 1
        col = 0
        while idx < len(self._graphemes) and col < self.cursor.col:
            if self._graphemes[idx] == "\n":
                break
            idx += 1
            col += 1
        return idx

    def _set_cursor_from_index(self, idx: int) -> None:
        """Inverse of ``_cursor_to_index``."""
        idx = max(0, min(idx, len(self._graphemes)))
        line = 0
        col = 0
        for i in range(idx):
            if self._graphemes[i] == "\n":
                line += 1
                col = 0
            else:
                col += 1
        self.cursor = Cursor(line=line, col=col)
