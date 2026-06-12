"""
File open / export helpers for the renderers' File-menu features.

Deliberately display-free (no tkinter, no prompt_toolkit) so the whole
module is testable in environments without a display - same rule as
``renderer/preview.py``.

Open semantics: the chosen file's text REPLACES the local block (the
renderer calls ``engine.set_local_text``, which emits a Snapshot, so the
new content broadcasts to peers for free and the normal autosave persists
it to ``mine.txt``). Because the content fans out to every peer over the
mesh, opening is guarded: files over ``MAX_OPEN_BYTES`` and binary files
(NUL byte sniff) are rejected with ValueError rather than silently
flooding the LAN with garbage.

Decoding: UTF-8 first (``utf-8-sig`` so a BOM doesn't surface as a
visible \\ufeff character), falling back to cp1252 - Matthew's boxes are
Windows and legacy Notepad files are the realistic non-UTF-8 case.
cp1252 maps every byte, so the fallback never raises; worst case is
mojibake, which the user can see and undo by just not keeping it.
Newlines are normalized to ``\\n`` (the engine, Tk Text, and the term
renderer all speak ``\\n``; ``\\r`` survivors would render as artifacts
and corrupt grapheme-position arithmetic between peers).

Export: atomic temp+rename, UTF-8, mirrors ``Document.save``. Exports
``\\n`` line endings; modern Windows Notepad reads them fine.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# An open file's text becomes our block and is snapshot-broadcast to every
# peer; keep that sane. 5 MB of text is ~80k lines - far beyond scratchpad
# territory already.
MAX_OPEN_BYTES = 5 * 1024 * 1024


def read_text_for_open(path: Path) -> str:
    """Read ``path`` for File->Open. Returns normalized text.

    Raises OSError (missing / unreadable) or ValueError (too big /
    binary). Callers surface the message as a transient and move on.
    """
    p = Path(path).expanduser()
    data = p.read_bytes()
    if len(data) > MAX_OPEN_BYTES:
        raise ValueError(
            "file too large to open ("
            + str(len(data)) + " > " + str(MAX_OPEN_BYTES) + " bytes)"
        )
    if b"\x00" in data:
        raise ValueError("binary file (contains NUL bytes)")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def export_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp + rename), UTF-8.

    ``~`` is expanded; parent directories are created (same convenience
    as ``Document.save``). Raises OSError on failure.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def block_choices(
    own_label: str, own_text: str, peers: Iterable[object]
) -> list[tuple[str, str]]:
    """Ordered (label, text) pairs for an export picker: own block first,
    then peers that have actually sent content (``has_received_snapshot``)
    - tombstoned peers included, since exporting a departed peer's
    last-known text is a legitimate rescue. ``peers`` is duck-typed
    (hostname / block_text / has_received_snapshot) to stay import-light;
    pass ``engine.sorted_peers()`` for display order.
    """
    choices = [(own_label, own_text)]
    for p in peers:
        if getattr(p, "has_received_snapshot", False):
            choices.append((p.hostname, p.block_text))
    return choices


# Windows-reserved filename characters (plus control chars). mDNS instance
# names are looser than filenames; a peer named e.g. ``box: kitchen`` must
# not blow up the default export filename.
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(label: str) -> str:
    """``label`` reduced to something safe as a suggested filename stem."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", label).strip(" .")
    return cleaned or "block"
