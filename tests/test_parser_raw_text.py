"""Raw-text and RCDATA content (script, style, textarea, title) tokenises the same on every Python.

The standard library's HTML tokeniser changed how it treats these elements between releases (a
textarea or title kept its markup as text only from later 3.13 releases on), so the parser reads their
content itself. The tests run each case under both stdlib behaviours.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from bgwcli.parser import parse_document, parse_page


@pytest.fixture(params=["stdlib-rcdata", "stdlib-markup"])
def stdlib_behaviour(request, monkeypatch):
    """Simulate a stdlib that treats textarea/title as RCDATA (3.14) and one that treats them as markup (<= 3.13.5)."""
    if request.param == "stdlib-markup":
        monkeypatch.setattr(HTMLParser, "RCDATA_CONTENT_ELEMENTS", (), raising=False)
        monkeypatch.setattr(HTMLParser, "CDATA_CONTENT_ELEMENTS", ("script", "style"), raising=False)
    return request.param


def test_textarea_content_is_text_with_entities_decoded(stdlib_behaviour):
    page = parse_page("x", "<textarea name=a><b>x</b> &amp; y</textarea>")
    assert page.textareas[0].value == "<b>x</b> & y"
    assert page.values["Field a"] == "<b>x</b> & y"


def test_title_content_is_text(stdlib_behaviour):
    page = parse_page("x", "<title>A &amp; <b>B</title><p>after")
    assert page.title == "A & <b>B"


def test_comment_markers_inside_textarea_and_title_are_text(stdlib_behaviour):
    page = parse_page("x", "<textarea name=t><!-- x --></textarea><title><!-- y --></title>")
    assert page.textareas[0].value == "<!-- x -->"
    assert page.title == "<!-- y -->"


def test_script_and_style_content_is_not_page_text(stdlib_behaviour):
    page = parse_page("x", "<script>if(a<b){}</script><style>td{}</style><table><tr><td>k</td><td>v</td></tr></table>")
    assert page.values == {"k": "v"}
    assert parse_document("<script>a<b>c</script>").find_all("b") == []


def test_content_ends_only_at_the_matching_close_tag(stdlib_behaviour):
    root = parse_document("<textarea name=a></b></textarea ><td>x</td>")
    assert [el.tag for el in root.find_all("textarea")] == ["textarea"]
    assert root.find_all("textarea")[0].raw_text() == "</b>"
    assert len(root.find_all("td")) == 1
    upper = parse_document("<TEXTAREA name=a>q</TEXTAREA><td>x</td>")
    assert upper.find_all("textarea")[0].raw_text() == "q"
    assert len(upper.find_all("td")) == 1


def test_an_unterminated_textarea_owns_the_rest_of_the_document(stdlib_behaviour):
    root = parse_document("<textarea name=a>one <td>two")
    assert root.find_all("textarea")[0].raw_text() == "one <td>two"
    assert root.find_all("td") == []


def test_a_self_closed_textarea_has_no_content(stdlib_behaviour):
    root = parse_document('<textarea name="a"/><td>x</td>')
    assert root.find_all("textarea")[0].raw_text() == ""
    assert len(root.find_all("td")) == 1


def test_script_end_tag_inside_a_string_ends_the_script_like_a_browser(stdlib_behaviour):
    root = parse_document("<script>document.write('</scr'+'ipt>')</script><td>x</td>")
    assert len(root.find_all("td")) == 1
