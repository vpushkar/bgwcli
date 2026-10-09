"""HTML text helper shared by the parser: whitespace normalization.

Entity decoding is done by the stdlib ``html.parser.HTMLParser`` (``convert_charrefs=True``) that the
parser builds on; this module only collapses whitespace the same way src/html.ts did.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")


def normalize_whitespace(value: str) -> str:
    """Collapse all whitespace (including U+00A0) to single spaces and trim."""
    return _WHITESPACE.sub(" ", value).strip()
