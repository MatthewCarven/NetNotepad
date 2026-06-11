"""
Tests for netnotepad/renderer/preview.py — the display-free half of the
inline image previews. The Tk wiring itself (embeds in the peers pane,
the own-pane strip) needs a real display and is verified live on the LAN,
like all Tk behaviour in this project.
"""

from __future__ import annotations

from netnotepad.engine.attachments import make_attachment_token
from netnotepad.renderer import preview


class FakeStore:
    def __init__(self, cached):
        self.cached = set(cached)

    def has(self, sha):
        return sha in self.cached


def test_is_previewable_image_native_formats():
    assert preview.is_previewable_image("photo.png")
    assert preview.is_previewable_image("anim.GIF")  # case-insensitive
    assert not preview.is_previewable_image("notes.txt")
    assert not preview.is_previewable_image("archive.zip")
    assert not preview.is_previewable_image("no_extension")


def test_is_previewable_image_pil_formats_track_availability():
    # jpeg is only previewable when Pillow is around; the helper must agree
    # with the module's availability probe either way.
    assert preview.is_previewable_image("pic.jpg") == preview.HAVE_PIL
    assert preview.is_previewable_image("pic.webp") == preview.HAVE_PIL


def test_subsample_factor_clamps_and_ceils():
    d = preview.MAX_PREVIEW_DIM
    assert preview.subsample_factor(10, 10) == 1          # already small
    assert preview.subsample_factor(d, d) == 1            # exactly fits
    assert preview.subsample_factor(d + 1, 10) == 2       # just over -> ceil
    assert preview.subsample_factor(10, 4 * d) == 4       # height drives it
    assert preview.subsample_factor(0, 0) == 1            # degenerate input


def test_cached_preview_shas_filters_dedupes_keeps_order():
    sha_img1, sha_img2, sha_txt, sha_uncached = (
        "1" * 64, "2" * 64, "3" * 64, "4" * 64,
    )
    text = " ".join(
        [
            make_attachment_token(sha_img1, "a.png"),
            make_attachment_token(sha_txt, "notes.txt"),      # not an image
            make_attachment_token(sha_img2, "b.gif"),
            make_attachment_token(sha_img1, "a.png"),         # duplicate
            make_attachment_token(sha_uncached, "c.png"),     # not cached
        ]
    )
    store = FakeStore({sha_img1, sha_img2, sha_txt})
    assert preview.cached_preview_shas(text, store) == (sha_img1, sha_img2)


def test_cached_preview_shas_empty_and_none_like_input():
    store = FakeStore(set())
    assert preview.cached_preview_shas("", store) == ()
    assert preview.cached_preview_shas("no tokens here", store) == ()
