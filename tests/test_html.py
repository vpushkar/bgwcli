"""Tests for bgwcli.html — whitespace normalization the parser relies on."""

from bgwcli.html import normalize_whitespace


def test_normalize_whitespace_collapses_and_trims():
    assert normalize_whitespace("  a \n\t b  c  ") == "a b c"
    assert normalize_whitespace("") == ""


def test_normalize_whitespace_treats_nbsp_as_whitespace():
    assert normalize_whitespace("Rx  Power ") == "Rx Power"
