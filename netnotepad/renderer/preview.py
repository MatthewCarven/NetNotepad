"""
Display-free helpers for attachment image previews.

The Tk renderer uses these to decide which attachment tokens get an inline
thumbnail and how hard a tk.PhotoImage needs to be downscaled. They live in
their own module (no tkinter import) so the headless test suite can cover the
logic — the sandbox/CI environment has no tkinter, let alone a display.

Pillow is optional. With it installed previews cover jpeg/webp/bmp/tiff and
scale smoothly; without it we're limited to the formats tk.PhotoImage reads
natively (png/gif/ppm/pgm) and crude integer subsampling. Both paths are
fine for a scratchpad — Pillow is an upgrade, not a requirement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netnotepad.engine.attachments import parse_attachment_tokens

# The longer side of a preview thumbnail, in pixels.
MAX_PREVIEW_DIM = 280

try:  # optional dependency
    from PIL import Image, ImageTk  # noqa: F401  (imported for availability probe)
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# Formats tk.PhotoImage can read without help (Tk 8.6+).
TK_NATIVE_EXTS = {".png", ".gif", ".ppm", ".pgm"}
# What Pillow adds on top.
PIL_EXTS = TK_NATIVE_EXTS | {".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".ico"}


def previewable_exts() -> set[str]:
    """The set of file extensions we can currently render as previews."""
    return PIL_EXTS if HAVE_PIL else TK_NATIVE_EXTS


def is_previewable_image(filename: str) -> bool:
    """True if ``filename`` looks like an image we can thumbnail right now."""
    return Path(filename).suffix.lower() in previewable_exts()


def subsample_factor(width: int, height: int, max_dim: int = MAX_PREVIEW_DIM) -> int:
    """Integer factor for ``PhotoImage.subsample`` so the larger side fits
    ``max_dim``. Always >= 1 (subsample(1,1) is the identity)."""
    m = max(width, height, 1)
    return max(1, -(-m // max_dim))  # ceil division


def cached_preview_shas(block_text: str, store: Any) -> tuple[str, ...]:
    """The shas in ``block_text`` worth previewing, in order of first
    appearance, de-duplicated: attachment tokens whose filename is a
    renderable image AND whose blob is already in the local store. Uncached
    blobs are simply skipped — the caller re-checks later (prefetch may
    still be in flight)."""
    out: list[str] = []
    for sha, name, _s, _e in parse_attachment_tokens(block_text or ""):
        if sha not in out and is_previewable_image(name) and store.has(sha):
            out.append(sha)
    return tuple(out)
