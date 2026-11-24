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
    # A <button> without a value attribute posts its caption, as the planner reads it.
    assert forms == [("/cgi-bin/a.ha", ["aa11"], ["x", "Go", "nonce"], {"Go": "Go"})]


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


# home.ha on fw 6.35.8 (captured 2026-11-23): each Wi-Fi Restart form opens a second <form> inside
# itself. Browsers ignore a <form> start tag while a form is open (HTML5 form-element pointer), so the
# button and BOTH nonces belong to the outer wrestart.ha?N form and are posted there together.
LIVE_HOME_WIFI = (
    "<title>Status</title>"
    '<form method="post" action="/cgi-bin/crestart.ha?1"><input type="hidden" name="nonce" value="cc0001" />'
    '<input type="submit" name="Broadband" class="cssbtn" value="Restart" /></form>'
    + "".join(
        f'<form method="post" action="/cgi-bin/wrestart.ha?{n}">'
        f'<input type="hidden" name="nonce" value="{outer}" />'
        "<!-- This is to avoid error : User should not request this page directly -->"
        '<form method="post" action="/cgi-bin/wrestart.ha">'
        f'<input type="hidden" name="nonce" value="{inner}" />'
        f'<input type="submit" name="WRestart{n}" class="cssbtn" value="Restart" />'
        "</form></form>"
        for n, outer, inner in ((1, "aa0001", "aa0002"), (2, "bb0001", "bb0002"))
    )
)


def test_a_form_opened_inside_an_open_form_is_ignored_like_a_browser_does():
    actions = [form[0] for form in _scan_forms(LIVE_HOME_WIFI)]
    assert actions == ["/cgi-bin/crestart.ha?1", "/cgi-bin/wrestart.ha?1", "/cgi-bin/wrestart.ha?2"]


def test_live_nested_wifi_restart_form_posts_both_of_its_nonces():
    error, posts = run(LIVE_HOME_WIFI, "wrestart.ha?1", {"WRestart1": "Restart"})
    assert error is None
    assert posts == [{"WRestart1": ["Restart"], "nonce": ["aa0001", "aa0002"]}]
    error, posts = run(LIVE_HOME_WIFI, "wrestart.ha?2", {"WRestart2": "Restart"})
    assert error is None
    assert posts == [{"WRestart2": ["Restart"], "nonce": ["bb0001", "bb0002"]}]


def test_a_commented_out_form_opener_never_captures_the_next_form():
    page = (
        "<title>Status</title>"
        '<!-- <form method="post" action="/cgi-bin/wrestart.ha?1"> -->'
        '<script>var t = \'<form action="/cgi-bin/wrestart.ha?1">\';</script>'
        f'<form method="post" action="/cgi-bin/wrestart.ha?2">{nonce_input("dd0002")}'
        '<input type="submit" name="WRestart2" value="Restart"></form>'
    )
    assert [form[0] for form in _scan_forms(page)] == ["/cgi-bin/wrestart.ha?2"]
    error, posts = run(page, "wrestart.ha?2", {"WRestart2": "Restart"})
    assert error is None and posts == [{"WRestart2": ["Restart"], "nonce": ["dd0002"]}]
    error, posts = run(page, "wrestart.ha?1", {"WRestart1": "Restart"})
    assert error is not None and posts == []


def test_a_button_a_browser_would_post_elsewhere_never_borrows_the_targets_other_form_nonce():
    # The inner restart.ha opener is ignored (the Restart button belongs to other.ha's form, as in a
    # browser); the only form posting to restart.ha holds Cancel. Its nonce is not Restart's.
    page = (
        "<title>Restart</title>"
        f'<form method="post" action="/cgi-bin/other.ha">{nonce_input("ee0001")}'
        '<form method="post" action="/cgi-bin/restart.ha"><input type="submit" name="Restart" value="Restart">'
        "</form></form>"
        f'<form method="post" action="/cgi-bin/restart.ha">{nonce_input("ee0002")}'
        '<input type="submit" name="Cancel" value="Cancel"></form>'
    )
    error, posts = run(page, "restart.ha", {"Restart": "Restart"}, nonce_page="restart")
    assert error is not None and posts == []
