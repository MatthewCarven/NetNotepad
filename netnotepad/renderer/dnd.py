"""Display-free helpers for drag-and-drop file attaching (Tk renderer).

The actual drop wiring lives in ``tk_renderer.py`` and depends on the
optional ``tkinterdnd2`` package (graceful fallback: no dep, no drop, the
Attach button still works). The one bug-prone bit — turning the string a
``<<Drop>>`` event hands us into a clean list of paths — is pulled out here
so it can be unit-tested without a display, in keeping with the project's
engine/renderer testability seam (cf. ``preview.py``).

``tkdnd`` delivers a DND_Files payload as a **Tcl list string**: paths are
space-separated, and any path containing whitespace is wrapped in ``{}``.
Examples seen in the wild::

    C:\\Users\\me\\a.png                         # single, no spaces
    {C:\\Users\\me\\my holiday.png}              # single, has spaces
    C:\\a.png {C:\\b c.png} D:\\d.png            # several, mixed

``parse_drop_paths`` reproduces just enough of Tcl's list splitting to
handle that format. We deliberately avoid leaning on
``root.tk.splitlist`` so the logic is testable headless and behaves
identically whether or not a Tcl interpreter is in the loop.
"""

from __future__ import annotations

__all__ = ["parse_drop_paths"]


def parse_drop_paths(data: str) -> list[str]:
    """Split a tkdnd DND_Files payload into individual path strings.

    Brace-wrapped elements (paths containing spaces) are unwrapped; bare
    elements are split on whitespace. Empty tokens are dropped. A
    malformed, unterminated ``{`` takes the rest of the string as one
    path rather than raising — a dropped file is never worth a crash.

    The returned strings are raw paths exactly as tkdnd reported them
    (native separators, no normalization); the caller decides what counts
    as a real, attachable file.
    """
    if not data:
        return []

    paths: list[str] = []
    i = 0
    n = len(data)
    while i < n:
        # Skip the whitespace between elements.
        while i < n and data[i].isspace():
            i += 1
        if i >= n:
            break

        if data[i] == "{":
            # Brace-delimited element: everything up to the next '}'.
            # DND_Files never nests braces, so a flat search is correct.
            end = data.find("}", i + 1)
            if end == -1:
                # Unterminated brace — salvage the remainder as one path.
                tail = data[i + 1:].strip()
                if tail:
                    paths.append(tail)
                break
            paths.append(data[i + 1:end])
            i = end + 1
        else:
            # Bare element: up to the next run of whitespace.
            j = i
            while j < n and not data[j].isspace():
                j += 1
            paths.append(data[i:j])
            i = j

    return [p for p in paths if p]
