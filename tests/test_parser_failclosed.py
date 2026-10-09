"""A secret never survives a layout the parser cannot read exactly: two-column header tables, spans and
text past the parser's bounds, cell-less rows, controls beside a secret label, and every text the parser
derives from a governed cell (labels, buttons, links, sitemap entries)."""

from __future__ import annotations

import io
import json

import pytest

from bgwcli.format import parsed_page_output, print_parsed_page
from bgwcli.parser import MAX_GRID_CELLS, MAX_TEXT_PARTS, parse_page
from bgwcli.types import to_json_dict

SECRET = "hunter2Zeta"


def everything(html: str, page: str = "x") -> str:
    """Every default output of a parse: the object, its JSON, and the terminal views."""
    parsed = parse_page(page, html)
    text = io.StringIO()
    print_parsed_page(parsed, stream=text)
    print_parsed_page(parsed, forms=True, stream=text)
    views = [repr(parsed), json.dumps(to_json_dict(parsed)), json.dumps(parsed_page_output(parsed)), text.getvalue()]
    return "\n".join(views)


def test_a_two_column_table_with_a_secret_header_redacts_its_column():
    th = f"<table><tr><th>Name</th><th>Password</th></tr><tr><td>home</td><td>{SECRET}</td></tr></table>"
    td = f"<table><tr><td>Name</td><td>Password</td></tr><tr><td>home</td><td>{SECRET}</td></tr></table>"
    for html in (th, td):
        assert SECRET not in everything(html)
        assert parse_page("x", html).values["home"] == "[redacted]"


def test_a_single_column_table_under_a_secret_header_redacts_its_cells():
    html = f"<table><tr><th>Passphrase</th></tr><tr><td>{SECRET}</td></tr></table>"
    assert SECRET not in everything(html)


def test_a_two_column_table_is_not_over_redacted_when_its_header_is_plain():
    html = "<table><tr><th>Name</th><th>Status</th></tr><tr><td>home</td><td>up</td></tr></table>"
    assert parse_page("x", html).values["home"] == "up"
    labelled = "<table><tr><td>Mode</td><td>Auto</td></tr><tr><td>Channel</td><td>6</td></tr></table>"
    assert parse_page("x", labelled).values == {"Mode": "Auto", "Channel": "6"}


# --- bounds fail closed ---------------------------------------------------------------------------

PAD_ROWS = '<tr><td colspan="64">a</td></tr>' * (MAX_GRID_CELLS // 64 + 40)
SECRET_ROW_CONTROLS = (
    f'<tr><td>Wi-Fi Password</td><td><input name="foo" value="{SECRET}"><select name="bar">'
    f'<option value="{SECRET}" selected>{SECRET}</option></select><textarea name="baz">{SECRET}</textarea></td></tr>'
)


def test_controls_past_a_cut_grid_are_redacted_and_sensitive():
    html = f"<table>{PAD_ROWS}{SECRET_ROW_CONTROLS}</table>"
    page = parse_page("x", html)
    assert page.truncated is True
    assert SECRET not in everything(html)
    assert [f.sensitive for f in page.fields] == [True]
    assert page.selects[0].sensitive and page.textareas[0].sensitive


def test_text_past_a_cut_grid_is_not_read_at_all():
    html = f"<table>{PAD_ROWS}<tr><td>Wi-Fi Password</td><td>{SECRET}</td></tr></table>"
    assert SECRET not in everything(html)


def test_controls_before_the_cut_of_a_grid_keep_their_plain_values():
    html = f'<table><tr><td>Mode</td><td><input name="mode" value="auto"></td></tr>{PAD_ROWS}</table>'
    page = parse_page("x", html)
    assert page.truncated is True
    assert [(f.name, f.value, f.sensitive) for f in page.fields] == [("mode", "auto", False)]


def test_a_clamped_secret_colspan_header_redacts_the_columns_after_it():
    inputs = "".join(f'<td><input name="f{n}" value="{SECRET}{n}"></td>' for n in range(100))
    html = f"<table><tr><th colspan=100>Password</th><th>Other</th></tr><tr>{inputs}<td>x</td></tr></table>"
    page = parse_page("x", html)
    assert page.truncated is True
    assert SECRET not in everything(html)


def test_a_clamped_colspan_does_not_move_a_secret_header_off_its_column():
    inputs = "".join(f'<td><input name="f{n}" value="{SECRET}{n}"></td>' for n in range(101))
    html = f"<table><tr><th colspan=100>Name</th><th>Password</th></tr><tr>{inputs}</tr></table>"
    assert SECRET not in everything(html)


def test_a_secret_label_whose_rowspan_is_clamped_keeps_governing_its_rows():
    rows = "".join(f"<tr><td>k{n}</td><td>{SECRET}{n}</td></tr>" for n in range(80))
    html = f"<table><tr><td rowspan=100>Password</td><td>first</td></tr>{rows}</table>"
    page = parse_page("x", html)
    assert page.truncated is True
    assert SECRET not in everything(html)


def test_a_span_too_long_to_convert_is_clamped_not_read_as_one():
    inputs = "".join(f'<td><input name="f{n}" value="{SECRET}{n}"></td>' for n in range(70))
    html = f'<table><tr><th colspan="{"9" * 5000}">Password</th></tr><tr>{inputs}</tr></table>'
    assert parse_page("x", html).truncated is True
    assert SECRET not in everything(html)


def test_a_table_with_a_clamped_span_and_no_secret_name_is_read_normally():
    html = (
        '<table><tr><td colspan="100">Mode</td><td>x</td></tr></table>'
        "<table><tr><td>Mode</td><td>Auto</td></tr></table>"
    )
    page = parse_page("x", html)
    assert page.truncated is True
    assert page.values["Mode"] == "Auto"


def test_a_secret_label_hidden_by_the_text_bound_still_governs_its_value():
    pad = "<b></b>" * (MAX_TEXT_PARTS + 1000)
    html = f'<table><tr><td>{pad}Wi-Fi Password</td><td><input name="foo" value="{SECRET}"></td></tr></table>'
    page = parse_page("x", html)
    assert page.truncated is True
    assert SECRET not in everything(html)
    assert page.fields[0].sensitive


def test_a_header_hidden_by_the_text_bound_still_governs_its_column():
    pad = "<b></b>" * (MAX_TEXT_PARTS + 1000)
    html = (
        f"<table><tr><th>Name</th><th>{pad}Passphrase</th><th>Other</th></tr>"
        f"<tr><td>home</td><td>{SECRET}</td><td>x</td></tr></table>"
    )
    assert SECRET not in everything(html)


def test_text_that_fits_the_bound_does_not_mark_the_page():
    html = "<table><tr><td>Note</td><td>" + "<i>w</i>" * 1000 + "</td></tr></table>"
    assert parse_page("x", html).truncated is None


# --- cell-less rows -------------------------------------------------------------------------------

EMPTY_ROW_LABEL = (
    "<table><tr><td rowspan=2>x</td><td>y</td></tr><tr></tr>"
    f'<tr><td>Password</td><td><input name="f" value="{SECRET}"></td></tr></table>'
)


def test_a_cell_less_row_still_counts_for_a_rowspan_carried_over_it():
    page = parse_page("x", EMPTY_ROW_LABEL)
    assert SECRET not in everything(EMPTY_ROW_LABEL)
    assert page.fields[0].sensitive


def test_a_cell_less_row_between_a_rowspan_and_a_secret_text_row():
    html = f"<table><tr><td rowspan=2>x</td><td>y</td></tr><tr></tr><tr><td>Password</td><td>{SECRET}</td></tr></table>"
    assert SECRET not in everything(html)


def test_a_leading_cell_less_row_does_not_hide_the_header_row():
    html = (
        f"<table><tr></tr><tr><th>Name</th><th>Password</th><th>Status</th></tr>"
        f"<tr><td>home</td><td>{SECRET}</td><td>on</td></tr></table>"
    )
    assert SECRET not in everything(html)
    assert parse_page("x", html).tables == [{"Name": "home", "Password": "[redacted]", "Status": "on"}]


def test_cell_less_rows_leave_the_records_of_an_ordinary_table_alone():
    html = (
        "<table><tr><th>A</th><th>B</th><th>C</th></tr><tr></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr><tr></tr></table>"
    )
    page = parse_page("x", html)
    assert page.tables == [{"A": "1", "B": "2", "C": "3"}]
    assert page.truncated is None


# --- controls inside the secret label cell --------------------------------------------------------

LABEL_CELL_CONTROLS = {
    "label then colon": f'<table><tr><td>Wi-Fi Password: <input name="foo" value="{SECRET}"></td></tr></table>',
    "label with a value cell": (
        f'<table><tr><td>Wi-Fi Password <input name="foo" value="{SECRET}"></td><td>x</td></tr></table>'
    ),
    "label default": (
        f'<table><tr><td>Password Default: <input name="foo" value="{SECRET}"></td><td>x</td></tr></table>'
    ),
    "select in the label": (
        f'<table><tr><td>Passphrase <select name="foo"><option selected>{SECRET}</option></select></td>'
        "<td>x</td></tr></table>"
    ),
    "textarea in the label": (
        f'<table><tr><td>Passphrase <textarea name="foo">{SECRET}</textarea></td><td>x</td></tr></table>'
    ),
}


@pytest.mark.parametrize("html", LABEL_CELL_CONTROLS.values(), ids=LABEL_CELL_CONTROLS.keys())
def test_a_control_inside_a_secret_label_cell_is_redacted_and_sensitive(html):
    page = parse_page("x", html)
    assert SECRET not in everything(html)
    controls = [*page.fields, *page.selects, *page.textareas]
    assert [c.sensitive for c in controls] == [True]


def test_a_control_beside_a_plain_label_keeps_its_value():
    html = '<table><tr><td>Mode <input name="foo" value="auto"></td><td>x</td></tr></table>'
    assert parse_page("x", html).fields[0].value == "auto"


# --- every derived string stays inside the secret scope --------------------------------------------

NESTED_LABEL = (
    f"<table><tr><td>Password</td><td><table><tr><td>{SECRET}</td><td>x</td></tr>"
    f"<tr><th>{SECRET}A</th><th>b</th><th>c</th></tr></table></td></tr></table>"
)


def test_a_table_nested_in_a_secret_cell_derives_no_labels_or_headers():
    page = parse_page("x", NESTED_LABEL)
    assert SECRET not in everything(NESTED_LABEL)
    assert page.values == {"Password": "[redacted]"} or all(SECRET not in k for k in page.values)


def test_a_nested_table_in_a_plain_cell_keeps_its_labels():
    html = "<table><tr><td>Info</td><td><table><tr><td>Mode</td><td>Auto</td></tr></table></td></tr></table>"
    assert parse_page("x", html).values["Mode"] == "Auto"


BUTTONS_IN_SECRET_CELL = {
    "input button": f'<input type="button" name="reveal" value="{SECRET}">',
    "input submit without a name": f'<input type="submit" value="{SECRET}">',
    "button element": f'<button name="reveal" value="{SECRET}">{SECRET}B</button>',
    "button element without a name": f'<button value="{SECRET}">cap{SECRET}</button>',
    "button caption only": f"<button>{SECRET}</button>",
}


@pytest.mark.parametrize("control", BUTTONS_IN_SECRET_CELL.values(), ids=BUTTONS_IN_SECRET_CELL.keys())
def test_buttons_inside_a_secret_cell_are_sensitive_and_redacted(control):
    html = f"<table><tr><td>Wi-Fi Password</td><td>{control}</td></tr></table>"
    page = parse_page("x", html)
    assert SECRET not in everything(html)
    assert [b.sensitive for b in page.buttons] == [True]


def test_buttons_outside_a_secret_cell_are_unchanged():
    html = '<table><tr><td>Mode</td><td><input type="submit" name="save" value="Save"></td></tr></table>'
    button = parse_page("x", html).buttons[0]
    assert (button.name, button.value, button.label, button.sensitive) == ("save", "Save", "Save", False)


def test_links_inside_a_secret_cell_are_redacted_wholesale():
    html = (
        f'<table><tr><td>Wi-Fi Password</td><td><a href="/x?v={SECRET}">go</a><a href="/cgi-bin/{SECRET}.ha"></a>'
        f'<a href="/y" title="{SECRET}">{SECRET}T</a></td></tr></table>'
    )
    page = parse_page("x", html)
    assert SECRET not in everything(html)
    assert all(link.href == "[redacted]" for link in page.links)
    assert all(link.page is None for link in page.links)


def test_links_outside_a_secret_cell_are_unchanged():
    html = '<table><tr><td>Mode</td><td><a href="/cgi-bin/home.ha?x=1">home</a></td></tr></table>'
    link = parse_page("x", html).links[0]
    assert (link.label, link.href, link.page) == ("home", "/cgi-bin/home.ha?x=1", "home")


def test_sitemap_anchors_inside_a_secret_cell_are_not_listed():
    anchor = f'<a href="/cgi-bin/{SECRET}.ha">{SECRET}</a>'
    html = f'<table><tr><td>Password</td><td>{anchor}</td></tr></table><a href="/cgi-bin/home.ha">Home</a>'
    from bgwcli.parser import parse_sitemap

    assert [(e.page, e.label) for e in parse_sitemap(html)] == [("home", "Home")]
    page = parse_page("sitemap", html)
    assert SECRET not in everything(html, page="sitemap")
    assert page.tables == [{"Page": "home", "Label": "Home", "Href": "/cgi-bin/home.ha"}]


def test_sitemap_of_an_ordinary_page_lists_every_link():
    html = '<a href="/cgi-bin/b.ha">B</a><a href="/cgi-bin/a.ha">A</a><a href="/other">x</a>'
    from bgwcli.parser import parse_sitemap

    assert [(e.page, e.label) for e in parse_sitemap(html)] == [("a", "A"), ("b", "B")]


@pytest.mark.parametrize(
    "name", ["%70assword", "pass%77ord", "%50ASSWORD", "wifi%5Fpassword", "%61ccess%20code", "to%6Ben"]
)
def test_an_encoded_secret_parameter_name_is_redacted_in_links_and_form_actions(name):
    html = (
        f'<form method="post" action="/cgi-bin/x.ha?{name}={SECRET}&page=2"><input name="a"></form>'
        f'<a href="/cgi-bin/y.ha?{name}={SECRET}&page=2">y</a>'
    )
    page = parse_page("x", html)
    assert SECRET not in everything(html)
    assert page.forms[0].action.endswith("&page=2")
    assert page.links[0].href.endswith("&page=2")


def test_a_plain_parameter_is_left_in_a_link():
    page = parse_page("x", '<a href="/cgi-bin/y.ha?page=%32&mode=a%20b">y</a>')
    assert page.links[0].href == "/cgi-bin/y.ha?page=%32&mode=a%20b"


def test_a_row_header_beside_a_value_is_a_label_not_a_column_header():
    html = f"<table><tr><th>Key</th><td>{SECRET}</td></tr><tr><td>Passphrase</td></tr></table>"
    assert SECRET not in everything(html)
    spanned = f'<table><tr><th rowspan="2">Key</th><td>{SECRET}</td></tr><tr><td>x</td></tr></table>'
    assert SECRET not in everything(spanned)


def test_a_narrow_header_row_whose_first_cell_names_a_secret_also_governs_its_value():
    html = f'<table><tr><th>Passphrase</th><th>Value<input name="v" value="{SECRET}"></th></tr></table>'
    assert SECRET not in everything(html)
