"""One fail-closed rule picks the write nonce: the form posting to the target, never another form's."""

from __future__ import annotations

from urllib.parse import parse_qs

from save_helpers import client_with, html

from bgwcli.client import NoncePageRefusedError, _scan_forms

TARGET = "wconfig.ha"


def nonce_input(value: str) -> str:
    return f'<input type="hidden" name="nonce" value="{value}">'


def run(page: str, post_path: str, fields: dict[str, str], nonce_page: str = "home"):
    """Real client over a one-page wire; returns (client call outcome, POST bodies)."""

    def handler(request, number):
        if request.method == "POST":
            return html("<title>Done</title>")
        return html(page)

    client, wire = client_with(handler)
    error = None
    try:
        client.post_form(nonce_page, post_path, fields)
    except NoncePageRefusedError as refusal:
        error = refusal
    posts = [r for r in wire.requests if r.method == "POST"]
    bodies = [parse_qs(p.body if isinstance(p.body, str) else p.body.decode()) for p in posts]
    return error, bodies


def test_scan_forms_collects_control_names_in_the_same_pass():
    forms = _scan_forms(
        '<form action="/cgi-bin/a.ha"><input name="x" value="1"><button name="Go">Go</button>'
        '<input name="nonce" value="aa11"></form>'
    )
    assert forms == [("/cgi-bin/a.ha", ["aa11"], ["x", "Go", "nonce"])]


# -- control gate -----------------------------------------------------------------------------------


def test_a_page_of_only_hidden_nonce_and_hashpassword_has_no_form_controls():
    page = f'<title>t</title><form action="/cgi-bin/{TARGET}">{nonce_input("aa11")}' \
        '<input type="hidden" name="hashpassword" value="x"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is not None and "no form controls" in str(error)
    assert posts == []


def test_control_gate_ignores_hidden_nonce_names_in_any_case():
    page = f'<title>t</title><form action="/cgi-bin/{TARGET}"><input type="hidden" name="Nonce" value="aa11">' \
        '<input type="hidden" name="HashPassword" value="x"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is not None and "no form controls" in str(error)
    assert posts == []


def test_the_same_page_with_one_button_is_accepted():
    page = f'<title>t</title><form action="/cgi-bin/{TARGET}">{nonce_input("aa11")}' \
        '<input type="submit" name="Continue" value="Continue"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is None
    assert posts == [{"Continue": ["Continue"], "nonce": ["aa11"]}]


# -- matching form without a nonce -----------------------------------------------------------------


def test_matching_form_with_nonce_outside_it_uses_the_page_nonce_when_it_is_the_only_form():
    page = f'<title>t</title>{nonce_input("aa11")}<form action="/cgi-bin/{TARGET}">' \
        '<input type="submit" name="Continue" value="Continue"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is None
    assert posts == [{"Continue": ["Continue"], "nonce": ["aa11"]}]


def test_matching_form_with_nonce_outside_it_is_refused_when_another_form_exists():
    page = f'<title>t</title>{nonce_input("aa11")}<form action="/cgi-bin/{TARGET}">' \
        '<input type="submit" name="Continue" value="Continue"></form>' \
        f'<form action="/cgi-bin/other.ha">{nonce_input("bb22")}<input type="submit" name="Other" value="x"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is not None and "carries no write nonce" in str(error) and TARGET in str(error)
    assert posts == []


# -- forms posting elsewhere -----------------------------------------------------------------------


def test_a_single_form_posting_to_another_cgi_is_refused_naming_both_actions():
    page = '<title>Status</title><form action="/cgi-bin/wrestart.ha?2">' \
        f'{nonce_input("cc33")}<input type="submit" name="WRestart2" value="Restart"></form>'
    error, posts = run(page, "wrestart.ha?1", {"WRestart1": "Restart"})
    assert error is not None
    assert "/cgi-bin/wrestart.ha?2" in str(error) and "wrestart.ha?1" in str(error)
    assert "another form's nonce is never sent" in str(error)
    assert posts == []


def test_home_with_five_restart_forms_posts_the_target_forms_nonce():
    home = "<title>Status</title>" + "".join(
        f'<form method="post" action="/cgi-bin/{action}">{nonces}'
        f'<input type="submit" name="{name}" value="Restart"></form>'
        for action, nonces, name in (
            ("crestart.ha?1", nonce_input("aaaa01"), "Broadband"),
            ("wrestart.ha?1", nonce_input("bbbb01") + nonce_input("bbbb02"), "WRestart1"),
            ("wrestart.ha?2", nonce_input("cccc01"), "WRestart2"),
            ("wrestart.ha?3", "", "WRestart3"),
            ("brestart.ha", "", "Gateway"),
        )
    )
    error, posts = run(home, "wrestart.ha?1", {"WRestart1": "Restart"})
    assert error is None
    assert posts == [{"WRestart1": ["Restart"], "nonce": ["bbbb01", "bbbb02"]}]


def test_a_relative_action_still_matches_the_absolute_target():
    page = f'<title>t</title><form action="{TARGET}">{nonce_input("aa11")}' \
        '<input type="submit" name="Continue" value="Continue"></form>'
    error, posts = run(page, TARGET, {"Continue": "Continue"})
    assert error is None
    assert posts == [{"Continue": ["Continue"], "nonce": ["aa11"]}]


# -- several forms posting to the same CGI ---------------------------------------------------------


def sibling_forms(cancel_nonce: str, continue_nonce: str) -> str:
    return (
        "<title>Warning</title>"
        f'<form action="/cgi-bin/{TARGET}">{nonce_input(cancel_nonce)}'
        '<input type="submit" name="Cancel" value="Cancel"></form>'
        f'<form action="/cgi-bin/{TARGET}">{nonce_input(continue_nonce)}'
        '<input type="submit" name="Continue" value="Continue"></form>'
    )


def test_sibling_forms_are_told_apart_by_the_button_being_posted():
    error, posts = run(sibling_forms("aaaa01", "bbbb01"), TARGET, {"Continue": "Continue"})
    assert error is None
    assert posts == [{"Continue": ["Continue"], "nonce": ["bbbb01"]}]


def test_sibling_forms_with_identical_nonces_post_once_with_that_nonce():
    error, posts = run(sibling_forms("aaaa01", "aaaa01"), TARGET, {"Other": "x"})
    assert error is None
    assert posts == [{"Other": ["x"], "nonce": ["aaaa01"]}]


def test_sibling_forms_with_different_nonces_and_an_unknown_button_are_refused():
    error, posts = run(sibling_forms("aaaa01", "bbbb01"), TARGET, {"Other": "x"})
    assert error is not None
    assert "2 forms post to /cgi-bin/wconfig.ha with different nonces" in str(error)
    assert "Other" in str(error)
    assert posts == []


def test_sibling_forms_both_owning_the_button_with_different_nonces_are_refused():
    page = (
        "<title>Warning</title>"
        f'<form action="/cgi-bin/{TARGET}">{nonce_input("aaaa01")}<input type="submit" name="Go" value="1"></form>'
        f'<form action="/cgi-bin/{TARGET}">{nonce_input("bbbb01")}<input type="submit" name="Go" value="2"></form>'
    )
    error, posts = run(page, TARGET, {"Go": "1"})
    assert error is not None and "different nonces" in str(error)
    assert posts == []


def test_the_only_owning_sibling_without_a_nonce_is_refused_not_borrowed_from_the_other():
    page = (
        "<title>Warning</title>"
        f'<form action="/cgi-bin/{TARGET}">{nonce_input("aaaa01")}<input type="submit" name="Cancel" value="x"></form>'
        f'<form action="/cgi-bin/{TARGET}"><input type="submit" name="Continue" value="y"></form>'
    )
    error, posts = run(page, TARGET, {"Continue": "y"})
    assert error is not None
    assert posts == []


# -- Wi-Fi Warning live shape ----------------------------------------------------------------------


def test_wifi_warning_page_posts_the_continue_forms_nonce():
    nonce_a, nonce_b = "a" * 64, "b" * 64
    page = (
        "<title>Warning</title>"
        f'<form action="/cgi-bin/wifiwarn_advanced.ha">{nonce_input(nonce_a)}'
        '<input type="submit" name="Cancel" value="Cancel"></form>'
        f'<form action="/cgi-bin/{TARGET}">{nonce_input(nonce_b)}'
        '<input type="submit" name="Continue" value="Continue"></form>'
    )
    error, posts = run(page, TARGET, {"Continue": "Continue"}, nonce_page="wifiwarn")
    assert error is None
    assert posts == [{"Continue": ["Continue"], "nonce": [nonce_b]}]


def test_a_page_with_no_controls_and_no_nonce_is_refused():
    error, posts = run("<title>t</title><p>nothing</p>", TARGET, {"Continue": "Continue"})
    assert error is not None
    assert posts == []
