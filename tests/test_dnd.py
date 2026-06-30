"""Unit tests for the display-free drop-path parser (drag-and-drop attach).

These run headless — no tkinter, no tkdnd — because parsing the tkdnd
DND_Files payload is pure string work. The Tk wiring that consumes the
result is verified live on the LAN.
"""

from netnotepad.renderer.dnd import parse_drop_paths


def test_empty_and_whitespace():
    assert parse_drop_paths("") == []
    assert parse_drop_paths("   ") == []
    assert parse_drop_paths("\t \n") == []


def test_single_bare_path():
    assert parse_drop_paths(r"C:\Users\me\a.png") == [r"C:\Users\me\a.png"]


def test_single_braced_path_with_spaces():
    assert parse_drop_paths(r"{C:\Users\me\my holiday.png}") == [
        r"C:\Users\me\my holiday.png"
    ]


def test_multiple_mixed():
    data = r"C:\a.png {C:\b c\pic two.png} D:\d.txt"
    assert parse_drop_paths(data) == [
        r"C:\a.png",
        r"C:\b c\pic two.png",
        r"D:\d.txt",
    ]


def test_multiple_all_braced():
    data = r"{C:\one two\a.png} {C:\three four\b.png}"
    assert parse_drop_paths(data) == [
        r"C:\one two\a.png",
        r"C:\three four\b.png",
    ]


def test_posix_forward_slash_paths():
    # tkdnd on Linux/macOS reports forward-slash paths.
    data = "/home/me/a.png {/home/me/my pics/b.png}"
    assert parse_drop_paths(data) == [
        "/home/me/a.png",
        "/home/me/my pics/b.png",
    ]


def test_surrounding_and_collapsed_whitespace():
    data = "   C:\\a.png    C:\\b.png   "
    assert parse_drop_paths(data) == [r"C:\a.png", r"C:\b.png"]


def test_braced_path_preserves_internal_spaces_exactly():
    # No stripping inside braces — a name with a trailing space is kept.
    assert parse_drop_paths("{a b }") == ["a b "]


def test_unterminated_brace_is_salvaged_not_crashed():
    # Malformed payload: take the remainder as one path rather than raise.
    assert parse_drop_paths(r"{C:\never closed\a.png") == [
        r"C:\never closed\a.png"
    ]


def test_bare_then_unterminated_brace():
    assert parse_drop_paths(r"C:\a.png {C:\b c") == [r"C:\a.png", r"C:\b c"]


def test_single_file_no_braces_with_unicode():
    assert parse_drop_paths("/home/me/café.png") == ["/home/me/café.png"]
