"""PyInstaller entry-point script.

PyInstaller wants a script file to bundle, not a -m package invocation, so
this exists purely to forward to ``netnotepad.__main__._entrypoint``.

Running this file directly is equivalent to ``python -m netnotepad``, which
is useful as a sanity check during development.
"""

from __future__ import annotations

import sys

from netnotepad.__main__ import _entrypoint


if __name__ == "__main__":
    sys.exit(_entrypoint())
