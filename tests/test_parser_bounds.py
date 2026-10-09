"""Hostile bodies (the client caps a body at 4 MiB) cannot make the parser spend quadratic time or
memory: spans are capped, a table's grid is bounded, a stray end tag costs nothing, and nesting
deeper than any router page stops being structure. Time limits are generous floors, not ratios."""

from __future__ import annotations

import json
import time

from bgwcli.parser import (
    MAX_ELEMENTS,
    MAX_GRID_CELLS,
    MAX_NESTING,
    MAX_TEXT_PARTS,
    Element,
    _TreeBuilder,
    parse_document,
    parse_page,
    span_value,
)
from bgwcli.types import to_json_dict

DEADLINE_SECONDS = 30.0  # gross-regression floor for slow machines; the bounds themselves are asserted structurally


def timed(html: str, page: str = "x"):
    started = time.perf_counter()
    parsed = parse_page(page, html)
    elapsed = time.perf_counter() - started
    assert elapsed < DEADLINE_SECONDS, f"parse took {elapsed:.1f}s"
    return parsed


def test_spans_are_capped_at_sixty_four():
    assert span_value("1000") == 64
    assert span_value("64") == 64
    assert span_value("65") == 64
    assert span_value("3") == 3


def test_colspan_amplification_is_bounded_and_marked_truncated():
    html = "<table>" + "<tr><td colspan=1000>x</td></tr>" * 5000 + "</table>"
    page = timed(html)
    assert page.truncated is True
    assert to_json_dict(page)["truncated"] is True


def test_many_short_rows_are_bounded_and_marked_truncated():
    html = "<table><tr><th>A<th>B<th>C" + "".join(f"<tr><td>{n}<td>b<td>c" for n in range(100000))
    page = timed(html + "</table>")
    assert page.truncated is True
    assert 0 < len(page.tables) <= MAX_GRID_CELLS // 3


def test_a_table_within_the_bound_is_not_marked():
    html = "<table><tr><th>A</th><th>B</th><th>C</th></tr>" + "<tr><td>a</td><td>b</td><td>c</td></tr>" * 1000
    page = parse_page("x", html + "</table>")
    assert page.truncated is None
    assert "truncated" not in to_json_dict(page)
    assert len(page.tables) == 1  # identical rows collapse


def test_the_bound_is_per_table():
    row = "<tr><td colspan=64>x</td></tr>"
    big = "<table>" + row * (MAX_GRID_CELLS // 64 + 10) + "</table>"
    small = "<table><tr><td>Mode</td><td>Auto</td></tr></table>"
    page = parse_page("x", big + small)
    assert page.truncated is True
    assert [e.label for e in page.value_entries if e.label == "Mode"] == ["Mode"]


def test_stray_end_tags_do_not_scan_the_open_stack(monkeypatch):
    reads = [0]

    class CountingStack(list):
        def __getitem__(self, index):
            reads[0] += 1
            return super().__getitem__(index)

    original = _TreeBuilder.__init__

    def init(self):
        original(self)
        self._stack = CountingStack(self._stack)

    monkeypatch.setattr(_TreeBuilder, "__init__", init)
    parse_document("<div>" * 20000 + "</b>" * 20000)
    assert reads[0] < 100000  # one read per element to open it; scanning would be 20000 x 20000


def test_a_matching_end_tag_still_closes_through_unclosed_children():
    root = parse_document("<div><b>one<i>two</b>three</div><p>four")
    div = root.children[0]
    assert [c.tag if hasattr(c, "tag") else c for c in div.children] == ["b", "three"]


def test_table_nesting_deeper_than_the_limit_stops_being_structure():
    root = parse_document("<table><tr><td>" * 2000 + "x")
    assert len(root.find_all("table")) == MAX_NESTING
    assert timed("<table><tr><td>" * 2000 + "x").tables == []


def test_heading_nesting_deeper_than_the_limit_stops_being_structure():
    html = "<h2>a<table><tr><td>k<td>v</table>" * 2000
    root = parse_document(html)
    assert len(root.find_all("h2")) == MAX_NESTING
    page = timed(html)
    assert all(set(entry.section.split()) == {"a"} for entry in page.value_entries)


def test_a_page_at_the_nesting_limit_is_unchanged():
    html = "<table><tr><td>" * (MAX_NESTING - 1) + "<table><tr><td>Mode</td><td>Auto</td></tr></table>"
    page = parse_page("x", html)
    assert [e.label for e in page.value_entries] == ["Mode"]
    assert page.truncated is None
    assert "Mode" in json.dumps(to_json_dict(page))


def test_a_document_with_more_than_the_element_bound_is_cut_and_marked_truncated():
    html = "<table><tr><td>Mode</td><td>Auto</td></tr></table>" + "<b>x</b>" * MAX_ELEMENTS
    page = timed(html)
    assert page.truncated is True
    assert [e.label for e in page.value_entries] == ["Mode"], "what came before the cut is read normally"
    assert len(parse_document(html).by_tag["b"]) <= MAX_ELEMENTS


def test_nested_cells_do_not_make_cell_text_quadratic(monkeypatch):
    steps = [0]
    original = Element._walk_text

    def counting(self, **kwargs):
        for item in original(self, **kwargs):
            steps[0] += 1
            yield item

    monkeypatch.setattr(Element, "_walk_text", counting)
    html = "<table><tr><td>Label<td>" * (MAX_NESTING - 2) + "<b>x</b>" * 60000
    parse_page("x", html)
    # Each cell's text read stops at the text bound; reading the whole 60000-element tail once per
    # nesting level would be over 15 million steps.
    assert steps[0] < 8_000_000


def test_a_cells_text_stops_at_the_text_bound():
    html = "<table><tr><td>Note</td><td>" + "<i>word</i> " * (MAX_TEXT_PARTS * 2) + "</td></tr></table>"
    page = parse_page("x", html)
    assert page.value_entries[0].value.startswith("word word")
    assert len(page.value_entries[0].value.split()) <= MAX_TEXT_PARTS
