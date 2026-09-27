# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""Allow the bundled macOS app to launch via ``python -m mac_cleaner``.

Briefcase starts the packaged GUI app with ``python -m mac_cleaner``. Without
this module that invocation fails at launch with::

    No module named mac_cleaner.__main__; 'mac_cleaner' is a package and
    cannot be directly executed

We therefore forward straight to the normal entry point and, if the desktop
GUI dependency (PySide6) is missing from the bundle, fall back to the Textual
terminal UI instead of crashing.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Run PoofMac, degrading gracefully when PySide6 is unavailable."""
    from mac_cleaner.main import run

    try:
        run()
    except ModuleNotFoundError as exc:
        if "PySide6" not in str(exc):
            raise
        print(
            "PySide6 (the desktop GUI) is not available in this build. "
            "Starting the terminal UI instead.",
            file=sys.stderr,
        )
        sys.argv.append("--tui")
        run()


if __name__ == "__main__":
    main()
