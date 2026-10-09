"""The fixture sanitizer reads layouts the way the parser does and fails closed: a page is never
certified as redacted while recognisable secret text survives."""

from __future__ import annotations

import pytest

from bgwcli.fixture_sanitize import contains_sensitive_fixture_value, sanitize_router_fixture, sensitive_control_residue
from bgwcli.parser import parse_page

SECRET = "hunter2Zeta"


def assert_clean(raw: str) -> str:
    sanitized = sanitize_router_fixture(raw)
    assert SECRET not in sanitized
    assert contains_sensitive_fixture_value(sanitized) is False
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []
    return sanitized


@pytest.mark.parametrize(
    "raw",
    [
        f"<table><tr><th>Name</th><th>Password</th></tr><tr><td>home</td><td>{SECRET}</td></tr></table>",
        f"<table><tr><td>Name</td><td>Password</td></tr><tr><td>home</td><td>{SECRET}</td></tr></table>",
    ],
    ids=["th header", "td header"],
)
def test_a_two_column_table_under_a_secret_header_is_sanitized(raw):
    assert contains_sensitive_fixture_value(raw) is True
    assert_clean(raw)


def test_a_cell_less_row_counts_for_a_rowspan_carried_over_it():
    raw = (
        "<table><tr><td rowspan=2>x</td><td>y</td></tr><tr></tr>"
        f'<tr><td>Password</td><td><input name="f" value="{SECRET}"></td></tr></table>'
    )
    assert_clean(raw)
    text_row = (
        f"<table><tr><td rowspan=2>x</td><td>y</td></tr><tr></tr><tr><td>Password</td><td>{SECRET}</td></tr></table>"
    )
    assert_clean(text_row)


@pytest.mark.parametrize(
    "raw",
    [
        f'<table><tr><td>Wi-Fi Password: <input name="foo" value="{SECRET}"></td></tr></table>',
        f'<table><tr><td>Wi-Fi Password <input name="foo" value="{SECRET}"></td><td>x</td></tr></table>',
        f'<table><tr><td>Password Default: <input name="foo" value="{SECRET}"></td><td>x</td></tr></table>',
        f'<table><tr><td>Passphrase <textarea name="foo">{SECRET}</textarea></td><td>x</td></tr></table>',
    ],
)
def test_a_control_inside_a_secret_label_cell_is_sanitized(raw):
    assert contains_sensitive_fixture_value(raw) is True
    assert_clean(raw)


# --- text of a control-bearing governed cell, targets, residue ---------------------------------------


def test_the_words_around_a_control_in_a_secret_cell_are_sanitized_and_agree_with_the_residue_check():
    raw = (
        f'<table><tr><td>Wi-Fi Password</td><td>{SECRET} <input name="foo" value="x"> (8-63 chars)</td>'
        "<td>y</td></tr></table>"
    )
    sanitized = assert_clean(raw)
    assert "8-63" not in sanitized
    assert 'value="[redacted]"' in sanitized
    hint_only = '<table><tr><td>Wi-Fi Password</td><td><input name="foo" value="x"> (8-63 chars)</td></tr></table>'
    assert contains_sensitive_fixture_value(hint_only) is True
    assert contains_sensitive_fixture_value(sanitize_router_fixture(hint_only)) is False


@pytest.mark.parametrize("name", ["password", "%70assword", "to%6Ben"])
def test_secret_query_values_in_form_actions_and_links_are_sanitized(name):
    raw = (
        f'<form method="post" action="/cgi-bin/x.ha?{name}={SECRET}&amp;page=2"><input name="a"></form>'
        f"<a href='/cgi-bin/y.ha?{name}={SECRET}&page=2'>y</a><a href=/cgi-bin/z.ha?{name}={SECRET}>z</a>"
    )
    assert contains_sensitive_fixture_value(raw) is True
    sanitized = assert_clean(raw)
    assert "page=2" in sanitized
    assert f"{name}=[redacted]" in sanitized


def test_a_plain_target_is_left_alone():
    raw = '<form action="/cgi-bin/x.ha?page=2"><a href="/cgi-bin/y.ha?mode=a">y</a></form>'
    assert sanitize_router_fixture(raw) == raw


def test_the_literal_pass_refuses_a_page_that_still_holds_a_found_secret():
    from bgwcli.audit import FixtureSafetyError, _assert_fixture_safe
    from bgwcli.fixture_sanitize import fixture_secret_values

    original = f'<form><input name="wifi_password" value="{SECRET}"></form>'
    secrets = fixture_secret_values(original)
    assert SECRET in secrets
    leaky = f"<p>current key {SECRET}</p>"
    with pytest.raises(FixtureSafetyError):
        _assert_fixture_safe("x", leaky, parse_page("x", leaky), secrets=secrets)
    clean = sanitize_router_fixture(original)
    _assert_fixture_safe("x", clean, parse_page("x", clean), secrets=secrets)


def test_residue_sees_a_sensitive_button_a_secret_link_and_a_secret_form_action():
    page = parse_page(
        "x",
        f'<table><tr><td>Password</td><td><input type="submit" value="{SECRET}"></td></tr></table>'
        f'<a href="/x?token={SECRET}">t</a><form action="/y?password={SECRET}"></form>',
        include_secrets=True,
    )
    assert {"link target", "form action"} <= set(sensitive_control_residue(page))
    assert len(sensitive_control_residue(page)) >= 3


def test_residue_does_not_read_a_sitemap_page_id_as_a_secret_label():
    """The gateway's sitemap always lists ``routerpasswd`` (Access Code) and ``ippass`` (IP Passthrough).
    A page id is an identifier, not the label of a secret value: the row holds a nav label and a path."""
    from bgwcli.audit import _assert_fixture_safe
    from bgwcli.fixture_sanitize import fixture_secret_values

    html = (
        '<ul><li><a href="/cgi-bin/routerpasswd.ha">Access Code</a></li>'
        '<li><a href="/cgi-bin/ippass.ha">IP Passthrough</a></li></ul>'
    )
    assert sensitive_control_residue(parse_page("sitemap", html, include_secrets=True)) == []
    sanitized = sanitize_router_fixture(html)
    assert sanitized == html
    _assert_fixture_safe("sitemap", sanitized, parse_page("sitemap", sanitized), secrets=fixture_secret_values(html))
    leaky = html + f'<a href="/cgi-bin/x.ha?password={SECRET}">x</a>'
    assert "link target" in sensitive_control_residue(parse_page("sitemap", leaky, include_secrets=True))


def _voice_page(line_value: str) -> str:
    return (
        "<table><thead><tr><th>Metric</th><th>Line 1</th><th>Line 2</th></tr></thead><tbody>"
        f"<tr><td>Phone Number</td><td>{line_value}</td><td>{line_value}</td></tr></tbody></table>"
        f"<div id='help'><strong>Phone Number:</strong> The Phone Number field displays '{line_value}' when the"
        " phone is not registered or ready for use.</div>"
    )


def test_the_literal_pass_lets_a_gateway_placeholder_quoted_by_the_help_text_through():
    """``Not Subscribed`` under Phone Number is the gateway's placeholder for an absent value, and the
    page's own help prose quotes it; it is not a secret. A real number in the same places still is."""
    from bgwcli.audit import FixtureSafetyError, _assert_fixture_safe
    from bgwcli.fixture_sanitize import fixture_secret_values

    original = _voice_page("Not Subscribed")
    secrets = fixture_secret_values(original)
    assert not any("Not Subscribed" in secret for secret in secrets)
    clean = sanitize_router_fixture(original)
    assert clean.count("<td>[redacted]</td>") == 2
    assert "displays 'Not Subscribed'" in clean
    _assert_fixture_safe("voice", clean, parse_page("voice", clean), secrets=secrets)

    real = _voice_page("555-0100")
    real_secrets = fixture_secret_values(real)
    assert "555-0100" in real_secrets
    leaky = sanitize_router_fixture(real)
    assert "displays '555-0100'" in leaky
    with pytest.raises(FixtureSafetyError):
        _assert_fixture_safe("voice", leaky, parse_page("voice", leaky), secrets=real_secrets)


@pytest.mark.parametrize("first_column", ["Line", "Section"])
def test_a_secret_value_in_a_wide_table_whose_first_column_names_the_row_is_still_residue(first_column):
    """A record whose first key is a row-naming key other than Metric (``Line``, ``Section``) gets an
    empty row label, so its values are not judged by the row's name; a secret header over one of its
    columns still names that value as residue through the (key, value) pairs. (The voice page itself
    parses positionally into Metric/Line 1/Line 2 and cannot produce this layout; the generic wide-table
    parse can, and that is the path the row-label rule runs on.)"""
    from bgwcli.audit import FixtureSafetyError, _assert_fixture_safe
    from bgwcli.fixture_sanitize import fixture_secret_values

    raw = (
        f"<table><thead><tr><th>{first_column}</th><th>Phone Number</th><th>Status</th></tr></thead><tbody>"
        "<tr><td>1</td><td>555-0100</td><td>Registered</td></tr>"
        "<tr><td>2</td><td>Not Subscribed</td><td>Idle</td></tr></tbody></table>"
    )
    parsed = parse_page("x", raw, include_secrets=True)
    assert parsed.tables and next(iter(parsed.tables[0])) == first_column, parsed.tables
    assert sensitive_control_residue(parsed) == ["Phone Number"]
    secrets = fixture_secret_values(raw)
    assert "555-0100" in secrets
    with pytest.raises(FixtureSafetyError):
        _assert_fixture_safe("x", raw, parse_page("x", raw), secrets=secrets)
    sanitized = sanitize_router_fixture(raw)
    assert "555-0100" not in sanitized
    assert sensitive_control_residue(parse_page("x", sanitized, include_secrets=True)) == []
    _assert_fixture_safe("x", sanitized, parse_page("x", sanitized), secrets=secrets)


def test_a_sensitive_button_in_a_secret_cell_is_sanitized():
    raw = (
        f'<table><tr><td>Password</td><td><button name="r" value="{SECRET}">{SECRET}B</button>'
        f'<input type="submit" value="{SECRET}C"></td></tr></table>'
    )
    assert_clean(raw)


# --- bounds -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "<table><tr><td>Password<td>" * 300,
        "<b>x</b>" * 160_000,
        "<table><tr><td>" + "<i>w</i>" * 6000 + "</td></tr></table>",
        "<table>" + '<tr><td colspan="64">a</td></tr>' * 4100 + "</table>",
        '<table><tr><td colspan="100">Password</td><td>x</td></tr></table>',
    ],
    ids=["nesting", "elements", "cell text", "grid", "span clamp"],
)
def test_a_page_past_a_parser_bound_is_refused_not_sanitized(raw):
    import time

    from bgwcli.fixture_sanitize import FixtureBoundsError

    started = time.perf_counter()
    with pytest.raises(FixtureBoundsError):
        sanitize_router_fixture(raw)
    assert time.perf_counter() - started < 30


def test_capture_refuses_a_page_past_a_bound_and_keeps_the_previous_fixture(tmp_path):
    import io

    from bgwcli.audit import capture_fixture_pack
    from bgwcli.sweep import SweepPage

    good = SweepPage(
        "Device", "Diag", "diag", False, False, True, raw_html="<table><tr><td>a</td><td>b</td></tr></table>"
    )
    assert capture_fixture_pack([good], tmp_path, stdout=io.StringIO()) == 1
    before = (tmp_path / "router-html" / "diag.html").read_bytes()
    hostile = SweepPage("Device", "Diag", "diag", False, False, True, raw_html="<table><tr><td>Password<td>" * 300)
    degraded: list[str] = []
    out = io.StringIO()
    assert capture_fixture_pack([hostile], tmp_path, stdout=out, degraded=degraded) == 0
    assert degraded == ["diag"]
    assert (tmp_path / "router-html" / "diag.html").read_bytes() == before


def test_capture_without_a_previous_fixture_writes_a_placeholder_for_a_refused_page(tmp_path):
    import io

    from bgwcli.audit import capture_fixture_pack
    from bgwcli.sweep import SweepPage

    hostile = SweepPage("Device", "Diag", "diag", False, False, True, raw_html="<table><tr><td>Password<td>" * 300)
    assert capture_fixture_pack([hostile], tmp_path, stdout=io.StringIO()) == 0
    assert (tmp_path / "router-html" / "diag.html").read_text().startswith("<!-- bgw fixture capture failed")


def test_nested_secret_tables_stay_cheap():
    import time

    raw = "<table><tr><td>Password<td>" * 250 + "x"
    started = time.perf_counter()
    sanitize_router_fixture(raw)
    assert time.perf_counter() - started < 10


def test_labelled_control_lookup_is_not_quadratic():
    import time

    rows = "".join(
        f'<tr><td>Wi-Fi Password</td><td><input name="k{n}" value="{SECRET}{n}"></td></tr>' for n in range(8000)
    )
    raw = f"<table>{rows}</table>"
    started = time.perf_counter()
    sanitized = sanitize_router_fixture(raw)
    assert time.perf_counter() - started < 15
    assert SECRET not in sanitized


def test_names_before_macs_are_consumed_once_and_match_the_plain_regex():
    import random
    import re

    from bgwcli.fixture_sanitize import _redact_names_before_macs

    old = re.compile(r"[^<>\r\n/]+(/\[redacted-mac\])")
    assert _redact_names_before_macs("a /[redacted-mac]b/[redacted-mac]") == (
        "[redacted-name]/[redacted-mac][redacted-name]/[redacted-mac]"
    )
    rng = random.Random(7)
    alphabet = ["a", "b", " ", "/", "<", ">", "\n", "/[redacted-mac]", "[redacted-mac]", "]"]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
        assert _redact_names_before_macs(text) == old.sub(r"[redacted-name]\1", text), text


def test_a_fifo_or_link_in_place_of_a_fixture_does_not_hang_the_capture_check(tmp_path):
    import os

    from bgwcli.audit import _has_real_fixture

    directory = tmp_path / "router-html"
    directory.mkdir()
    os.mkfifo(directory / "diag.html")
    assert _has_real_fixture(tmp_path, "diag") is True
    (directory / "x.html").symlink_to(tmp_path)
    assert _has_real_fixture(tmp_path, "x") is True
    assert _has_real_fixture(tmp_path, "missing") is False
