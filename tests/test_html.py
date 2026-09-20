"""Tests for bgwcli.html — entity decoding, whitespace normalization, text helpers.

Derived from src/html.ts behavior (no dedicated TS test file existed; these pin the helpers
the parser relies on).
"""

from bgwcli.html import clean_text, decode_entities, normalize_whitespace, strip_tags


def test_decode_entities_handles_named_numeric_and_hex():
    assert decode_entities("a &amp; b &lt;c&gt; &quot;d&quot; &apos;e&apos;") == "a & b <c> \"d\" 'e'"
    assert decode_entities("&#65;&#x42;&#X43;") == "ABC"


def test_decode_entities_maps_nbsp_to_plain_space():
    # TS entityMap maps nbsp to a regular space, not U+00A0.
    assert decode_entities("Rx&nbsp;Power") == "Rx Power"


def test_decode_entities_leaves_unknown_entities_alone():
    assert decode_entities("&bogus; stays") == "&bogus; stays"


def test_normalize_whitespace_collapses_and_trims():
    assert normalize_whitespace("  a \n\t b  c  ") == "a b c"
    assert normalize_whitespace("") == ""


def test_clean_text_decodes_then_normalizes():
    assert clean_text("  Rx Power&nbsp;&nbsp;Currently&#32;-141 ") == "Rx Power Currently -141"


def test_strip_tags_replaces_tags_with_spaces_and_drops_script_style():
    assert strip_tags("a<b>b</b>c") == "a b c"
    assert strip_tags("<script>var x = '<td>';</script>keep<style>p{}</style>") == "keep"
    assert strip_tags('<td><input type="submit" name="Remove_1" value="Remove"></td>') == ""
