"""Terminal-safe text for anything the router (or a message built from it) sends to a terminal.

Kept apart from the renderers so the modules the renderers import (snapshot, restore, the CLI error
path) can sanitise router text too without importing `format`.
"""

from __future__ import annotations

import re
import unicodedata

# An OSC sequence ends at its own BEL or ST terminator; its payload never spans a line or another
# escape, so an unterminated one is dropped to the end of its line and cannot swallow later text.
_OSC = re.compile("\x1b\\][^\x07\x1b\r\n]*(?:\x07|\x1b\\\\)?")
_CSI = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]")
_WHITESPACE_RUN = re.compile(r"[\n\t]+")


def is_terminal_hazard(ch: str) -> bool:
    """C0/DEL/C1 controls and Unicode format characters (category Cf: bidi embeddings, overrides
    and isolates U+202A-U+202E / U+2066-U+2069, zero-width characters, BOM) that can reorder or
    hide what a terminal shows."""
    code = ord(ch)
    return code < 32 or 127 <= code <= 159 or (code > 159 and unicodedata.category(ch) == "Cf")


def sanitize_terminal_text(value: str, *, single_line: bool = False) -> str:
    """Strip OSC/CSI escape sequences, control characters (keeping \\n and \\t) and Unicode
    format characters (bidi controls, zero-width characters) from router text."""
    sanitized = _CSI.sub("", _OSC.sub("", value))
    sanitized = "".join(ch for ch in sanitized if ch in "\n\t" or not is_terminal_hazard(ch))
    return _WHITESPACE_RUN.sub(" ", sanitized) if single_line else sanitized


def printable_error(error: BaseException) -> str:
    """``str(error)`` as one terminal-safe line: router text embedded in a failure message cannot
    carry escape sequences, control characters or bidi overrides onto stderr."""
    return sanitize_terminal_text(str(error), single_line=True)
