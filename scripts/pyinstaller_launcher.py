"""Entry script for the single-file executable (`install.sh binary`).

PyInstaller needs a top-level script; the package's own `__main__.py` cannot serve because its relative
import has no parent when run as a script. This file does nothing but hand over to the CLI."""

import sys

from bgwcli.cli import main

sys.exit(main())
