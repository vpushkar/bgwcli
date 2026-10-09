"""Secrets stay redacted in every text view the parser derives from a page (enclosing cells,
descriptions, headings, links), however a secret is laid out: nested tables, spanned label and
value cells, wide rows with a secret label, copies of a secret control's option text."""

import json
import time

import pytest

from bgwcli.parser import Element, layout_table, parse_page, span_value
from bgwcli.types import to_json_dict

NEEDLE = "alpha987Beta654"
WIDE = (
    "<table><tr><th>Name</th><th>Password</th><th>Status</th></tr>"
    f"<tr><td>Home</td><td>{NEEDLE}</td><td>on</td></tr></table>"
)
PAIR = f"<table><tr><td>Password</td><td>{NEEDLE}</td></tr></table>"
SPANNED = f'<table><tr><td rowspan="2">Password</td><td>first-value</td></tr><tr><td>{NEEDLE}</td></tr></table>'


def outer(inner: str) -> str:
    return f"<table><tr><td>Details</td><td>{inner}</td></tr></table>"


def dump(html: str, **kwargs) -> str:
    return json.dumps(to_json_dict(parse_page("sysinfo", html, **kwargs)))


LEAKY_LAYOUTS = {
    "nested wide table": outer(WIDE),
    "nested rowspan label": outer(SPANNED),
    "description wrapper": f'<div class="desc">{PAIR}</div>',
    "secret select copied into a cell": (
        "<table><tr><th>Name</th><th>Value</th><th>Status</th></tr><tr><td>Wi-Fi</td>"
        f'<td><select name="password"><option selected>{NEEDLE}</option></select></td><td>on</td></tr></table>'
    ),
    "secret textarea copied into a cell": (
        f'<table><tr><td>Notes</td><td><textarea name="passphrase">{NEEDLE}</textarea></td></tr></table>'
    ),
    "wide row with a secret label": (
        f"<table><tr><th>Item</th><th>A</th><th>B</th></tr><tr><td>Password</td><td>{NEEDLE}</td><td>x</td></tr></table>"
    ),
    "wide row with a secret label under an empty header": (
        "<table><tr><th></th><th>A</th><th>B</th></tr>"
        f"<tr><td>Phone Number</td><td>x</td><td>{NEEDLE}</td></tr></table>"
    ),
    "rowspan secret label over a wide table": (
        "<table><tr><th>Item</th><th>A</th><th>B</th></tr>"
        f"<tr><td rowspan=2>Password</td><td>{NEEDLE}</td><td>x</td></tr><tr><td>y</td><td>{NEEDLE}</td></tr></table>"
    ),
    "secret table inside a label cell": (
        f"<table><tr><td><table><tr><td>Password</td><td>{NEEDLE}</td></tr></table></td><td>val</td></tr></table>"
    ),
    "unclosed cells around a nested secret table": (
        f"<table><tr><td>Password<td><table><tr><td>x<td>{NEEDLE}</table></table>"
    ),
    "secret in the label cell itself": f"<table><tr><td>Password: {NEEDLE}</td><td>x</td></tr></table>",
    "secret in a one-cell row": outer(f"<table><tr><td>Access Code: {NEEDLE}</td></tr></table>"),
    "heading with a secret": f"<h1>Access Code Currently {NEEDLE}</h1>",
    "link query secret": f'<a href="/cgi-bin/x.ha?password={NEEDLE}&amp;page=2">Next</a>',
    "nested secret in a link": f'<a href="/cgi-bin/x.ha">{PAIR}</a>',
}


@pytest.mark.parametrize("html", LEAKY_LAYOUTS.values(), ids=LEAKY_LAYOUTS.keys())
def test_no_derived_view_carries_a_secret(html):
    redacted = dump(html)
    assert NEEDLE not in redacted
    assert "[redacted]" in redacted


@pytest.mark.parametrize("html", LEAKY_LAYOUTS.values(), ids=LEAKY_LAYOUTS.keys())
def test_include_secrets_keeps_every_view_readable(html):
    assert NEEDLE in dump(html, include_secrets=True)


def test_colspan_state_survives_a_rowspan_carry():
    html = (
        "<table><tr><th>Name</th><th>Password</th><th>Status</th></tr>"
        f'<tr><td colspan="2" rowspan="2">{NEEDLE}</td><td>first</td></tr><tr><td>second</td></tr></table>'
    )
    parsed = parse_page("sysinfo", html)
    assert parsed.tables == []
    assert NEEDLE not in dump(html)


def test_ordinary_rowspan_records_are_kept_beside_excluded_colspan_rows():
    html = (
        "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td colspan=2 rowspan=2>x</td><td>1</td></tr><tr><td>2</td></tr>"
        "<tr><td rowspan=2>y</td><td>3</td><td>4</td></tr><tr><td>5</td><td>6</td></tr></table>"
    )
    assert parse_page("x", html).tables == [
        {"A": "y", "B": "3", "C": "4"},
        {"A": "y", "B": "5", "C": "6"},
    ]


def test_layout_marks_carried_colspan_copies():
    rows = [["a", "b"], ["c"], ["d", "e"]]
    spans = {"a": (2, 2), "b": (1, 1), "c": (1, 1), "d": (1, 1), "e": (1, 1)}
    grid = layout_table(rows, lambda cell, name: spans[cell][0 if name == "colspan" else 1])
    assert [sorted(row.span_copies) for row in grid] == [[1], [1], []]
    assert span_value("2") == 2


def test_a_header_row_naming_a_secret_keeps_its_column_names():
    html = "<table><tr><th>Password</th><th>A</th><th>B</th></tr><tr><td>p</td><td>1</td><td>2</td></tr></table>"
    assert parse_page("x", html).tables == [{"Password": "[redacted]", "A": "1", "B": "2"}]


def _text_walk_steps(monkeypatch, html: str, *, include_secrets: bool) -> int:
    """Tree-walk steps (descendant text and descendant elements) the parser takes for ``html``: an
    operation count, so the guard does not depend on how fast the machine is."""
    steps = [0]
    original_text = Element._walk_text
    original_elements = Element.iter_elements

    def counting_text(self, **kwargs):
        for item in original_text(self, **kwargs):
            steps[0] += 1
            yield item

    def counting_elements(self, **kwargs):
        for item in original_elements(self, **kwargs):
            steps[0] += 1
            yield item

    with monkeypatch.context() as patch:
        patch.setattr(Element, "_walk_text", counting_text)
        patch.setattr(Element, "iter_elements", counting_elements)
        parse_page("x", html, include_secrets=include_secrets)
    return steps[0]


def _nested_secret_tables(depth: int) -> str:
    return "<table><tr><td>Password<td>" * depth + "x"


def test_nested_depth_redaction_is_not_superlinear(monkeypatch):
    assert parse_page("x", _nested_secret_tables(400)).value_entries
    redacted = _text_walk_steps(monkeypatch, _nested_secret_tables(400), include_secrets=False)
    clear = _text_walk_steps(monkeypatch, _nested_secret_tables(400), include_secrets=True)
    # Redaction costs a constant factor over the plain parse, not an extra pass per nesting level.
    assert redacted <= 2 * clear
    # Doubling the depth at most quadruples the work (reading nested text is inherently quadratic); a cubic
    # walk would multiply it by eight.
    shallow = _text_walk_steps(monkeypatch, _nested_secret_tables(100), include_secrets=False)
    deeper = _text_walk_steps(monkeypatch, _nested_secret_tables(200), include_secrets=False)
    assert deeper <= 5 * shallow


def test_many_band_headings_are_linear():
    band = "<h2>2.4 GHz</h2><table><tr><td>Current Channel</td><td>{} (20 MHz)</td></tr></table>"
    bands = "".join(band.format(i % 11 + 1) for i in range(3000))
    started = time.perf_counter()
    parsed = parse_page("wconfig_unified", bands)
    assert time.perf_counter() - started < 1
    assert len(parsed.tables) == 11  # identical bands collapse


def test_an_unclosed_heading_does_not_carry_the_next_table_into_the_section():
    html = (
        "<h2>Wireless<table><tr><td>Password</td><td>" + NEEDLE + "</td></tr>"
        "<tr><td>Channel</td><td>6</td></tr></table>"
    )
    page = parse_page("x", html)
    assert NEEDLE not in json.dumps(to_json_dict(page))
    assert {entry.section for entry in page.value_entries} == {"Wireless"}
    assert [entry.label for entry in page.value_entries] == ["Password", "Channel"]


def test_an_unclosed_heading_inside_a_secret_cell_stays_redacted():
    html = (
        "<table><tr><td>Password</td><td><h3>" + NEEDLE + "<table><tr><td>a</td><td>b</td></tr></table>"
        "</td></tr></table>"
    )
    assert NEEDLE not in json.dumps(to_json_dict(parse_page("x", html)))


def test_a_wide_first_row_without_header_cells_that_starts_with_a_secret_label_is_a_data_row():
    html = (
        f"<table><tr><td>Password</td><td>{NEEDLE}</td><td>b</td></tr>"
        "<tr><td>x</td><td>y</td><td>z</td></tr></table>"
    )
    page = parse_page("x", html)
    assert NEEDLE not in json.dumps(to_json_dict(page))
    assert page.tables == [{"Password": "[redacted]", "[redacted]": "z"}]


def test_a_wide_header_row_of_th_cells_still_names_its_columns():
    html = (
        "<table><tr><th>Name</th><th>Password</th><th>Status</th></tr>"
        "<tr><td>a</td><td>p</td><td>s</td></tr></table>"
    )
    assert parse_page("x", html).tables == [{"Name": "a", "Password": "[redacted]", "Status": "s"}]


def test_a_td_header_row_that_does_not_start_with_a_secret_label_names_its_columns():
    html = (
        "<table><tr><td>Name</td><td>Password</td><td>Status</td></tr>"
        "<tr><td>a</td><td>p</td><td>s</td></tr></table>"
    )
    assert parse_page("x", html).tables == [{"Name": "a", "Password": "[redacted]", "Status": "s"}]


def test_a_header_cell_carried_by_rowspan_into_a_secret_labelled_row_is_redacted():
    html = (
        f"<table><tr><th>H1</th><th rowspan=2>{NEEDLE}</th><th>H3</th></tr>"
        "<tr><td>Password</td><td>v</td></tr></table>"
    )
    assert NEEDLE not in json.dumps(to_json_dict(parse_page("x", html)))


LABELLED_CONTROLS = (
    "<form><table>"
    f'<tr><td>Wi-Fi Password</td><td><input name="x1" value="{NEEDLE}"></td></tr>'
    f'<tr><td>Network Key</td><td><select name="x2"><option value="{NEEDLE}-a" selected>{NEEDLE}-label</option>'
    f'<option value="{NEEDLE}-b">b</option></select></td></tr>'
    f'<tr><td>Access Code</td><td><textarea name="x3">{NEEDLE}</textarea></td></tr>'
    '<tr><td>Hostname</td><td><input name="x4" value="plain-host"></td></tr>'
    "</table></form>"
)


def test_a_control_in_a_cell_whose_row_label_names_a_secret_is_redacted_by_label():
    page = parse_page("x", LABELLED_CONTROLS)
    assert NEEDLE not in json.dumps(to_json_dict(page))
    assert {f.name: (f.sensitive, f.value) for f in page.fields} == {
        "x1": (True, "[redacted]"),
        "x4": (False, "plain-host"),
    }
    select = page.selects[0]
    assert select.sensitive
    assert select.value == "[redacted]"
    assert select.options == ["[redacted]", "[redacted]"]
    assert [(o.value, o.label) for o in select.option_details] == [("[redacted]", "[redacted]")] * 2
    assert page.textareas[0].sensitive
    assert page.textareas[0].value == "[redacted]"


def test_a_label_governed_control_is_flagged_sensitive_in_the_secret_parse_too():
    page = parse_page("x", LABELLED_CONTROLS, include_secrets=True)
    assert [f.name for f in page.fields if f.sensitive] == ["x1"]
    assert page.fields[0].value == NEEDLE
    assert page.selects[0].sensitive and page.textareas[0].sensitive


def test_a_form_action_redacts_a_secret_query_parameter():
    html = f'<form method="post" action="/cgi-bin/x.ha?password={NEEDLE}&page=2"><input name="a" value="1"></form>'
    page = parse_page("x", html)
    assert page.forms[0].action == "/cgi-bin/x.ha?password=[redacted]&page=2"
    assert NEEDLE not in json.dumps(to_json_dict(page))
    assert parse_page("x", html, include_secrets=True).forms[0].action.endswith(f"password={NEEDLE}&page=2")


def test_an_icon_link_title_inside_a_secret_cell_is_redacted_like_link_text():
    html = (
        f'<table><tr><td>Password</td><td><a href="/cgi-bin/y.ha" title="{NEEDLE}"></a></td></tr>'
        f'<tr><td>Help</td><td><a href="/cgi-bin/z.ha" title="Open help"></a></td></tr></table>'
    )
    page = parse_page("x", html)
    assert NEEDLE not in json.dumps(to_json_dict(page))
    assert {link.href: link.label for link in page.links} == {
        "[redacted]": "[redacted]",
        "/cgi-bin/z.ha": "Open help",
    }
    assert {link.label for link in parse_page("x", html, include_secrets=True).links} == {NEEDLE, "Open help"}
