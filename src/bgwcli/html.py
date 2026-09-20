"""HTML text helpers shared by the parser: entity decoding, whitespace normalization, tag stripping.

These mirror src/html.ts closely enough that text extracted from the same markup is identical:
tags become single spaces, ``&nbsp;`` becomes a plain space, and runs of whitespace collapse.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_SCRIPT_BLOCK = re.compile(r"<script\b[\s\S]*?</script>", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style\b[\s\S]*?</style>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(#x?[0-9a-f]+|[a-z]+);", re.IGNORECASE)
_NAMED_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


def decode_entities(value: str) -> str:
    """Decode the small entity set the TS CLI understood; unknown named entities are left as-is."""

    def replace(match: re.Match[str]) -> str:
        entity = match.group(1)
        lower = entity.lower()
        if lower.startswith("#x"):
            return chr(int(lower[2:], 16))
        if lower.startswith("#"):
            return chr(int(lower[1:], 10))
        return _NAMED_ENTITIES.get(lower, f"&{entity};")

    return _ENTITY.sub(replace, value)


def normalize_whitespace(value: str) -> str:
    """Collapse all whitespace (including U+00A0) to single spaces and trim."""
    return _WHITESPACE.sub(" ", value).strip()


def clean_text(value: str) -> str:
    """Decode entities in a raw HTML text fragment, then normalize whitespace."""
    return normalize_whitespace(decode_entities(value))


def strip_tags(value: str) -> str:
    """Drop script/style blocks, replace every tag with a space, then clean_text."""
    without_blocks = _STYLE_BLOCK.sub(" ", _SCRIPT_BLOCK.sub(" ", value))
    return clean_text(_TAG.sub(" ", without_blocks))
