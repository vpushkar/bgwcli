"""Fourth review round, fixed by simplifying:

- one HTML model for forms (the stdlib HTMLParser): entities, quoted attributes, comments, raw-text
  elements and disabled fieldsets are read the way a browser reads them;
- button-only named actions (restart, restart-from-resets, clear-device-list, packet-filter toggles)
  post exactly {button: the value the page renders} plus the form's nonce;
- one redirect resolver (against the POST URL, normalized origin with ports, full path) for the
  restart hand-off, the Wi-Fi Warning gate and the answer read;
- restart exits 0 only when the gateway clearly went down or served its restarting page; any
  ambiguous outcome exits 2: "restart may be in progress, check uptime".
"""

import http.client
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import get_action
from bgwcli.client import NoncePageRefusedError, _scan_forms, _select_write_nonces, extract_nonce
from bgwcli.restore import is_confirmation_redirect

ORIGIN = "http://router.local"
RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="Restart" /><input type="submit" name="Cancel" value="Cancel" /></form>'
)
RESET_FORM = (
    '<form method="post" action="/cgi-bin/reset.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="ResetIP" value="Reset IP"/><input type="submit" name="Restart" value="Restart"/>'
    '<input type="submit" name="Reset" value="Reset Device..."/></form>'
)
SCAN_FORM = (
    '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old">'
    '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
)
WARNING_PAGE = (
    '<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="aa01">'
    '<input type="submit" name="Continue" value="Continue"></form>'
    '<form method="post" action="/cgi-bin/wifiwarn_advanced.ha"><input type="hidden" name="nonce" value="aa02">'
    '<input type="submit" name="Cancel" value="Cancel"></form>'
)
UNCERTAIN = "restart may be in progress, check uptime"


def _pf(toggle: str) -> str:
    return (
        '<form method="post" action="/cgi-bin/packetfilter.ha"><input type="hidden" name="nonce" value="abc123" />'
        f'{toggle}<input type="submit" name="Complete_16" value="" title="Enable/Disable Rule" /></form>'
    )


def _cli(monkeypatch, capsys, argv, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else {}
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    first = wire.requests.index(posts[0]) if posts else len(wire.requests)
    gets_after = [urlsplit(r.url).path for r in wire.requests[first + 1 :] if r.method == "GET"]
    return code, out, posts, gets_after, client, captured


def _after_post(source, post_answer, after=lambda r: html("<title>Restarting</title>")):
    state = {"posted": False}

    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            state["posted"] = True
            return post_answer(request)
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        if state["posted"]:
            return after(request)
        return html(source)

    return handler


def _raise(exc):
    def answer(request):
        raise exc

    return answer


RESTART = ["action", "restart", "--commit", "--confirm", "RESTART"]


# --- one HTML model ---------------------------------------------------------------------------------


def test_entities_in_attribute_values_are_decoded_like_the_planner_reads_them():
    forms = _scan_forms(
        '<form action="/cgi-bin/devices.ha"><input name="nonce" value="aa11">'
        '<input type="submit" name="Clear" value="Clear &amp; Rescan"></form>'
    )
    assert forms[0][3] == {"Clear": "Clear & Rescan"}


@pytest.mark.parametrize(
    "decoy",
    [
        '<textarea name="t"></textareaX><form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="ee01">'
        "</textarea>",
        '<input type="hidden" name="tip" value="<script">',
        '<input type="hidden" name="tip" value="<!--">',
    ],
    ids=["textarea-end-tag-prefix", "script-in-attribute", "comment-in-attribute"],
)
def test_text_and_attribute_values_never_open_or_hide_forms(decoy):
    page = (
        f"{decoy}"
        '<form method="post" action="/cgi-bin/wrestart.ha?2"><input type="hidden" name="nonce" value="ee02" />'
        '<input type="submit" name="WRestart2" value="Restart" /></form>'
    )
    with pytest.raises(NoncePageRefusedError):
        _select_write_nonces(page, "home", "wrestart.ha?1", {"WRestart1": "Restart"})
    assert _select_write_nonces(page, "home", "wrestart.ha?2", {"WRestart2": "Restart"}) == ["ee02"]


def test_a_submit_without_a_value_carries_what_the_planner_would_post():
    forms = _scan_forms(
        '<form action="/cgi-bin/a.ha"><input name="nonce" value="aa11">'
        '<button name="Go">Go now</button><input type="submit" name="Plain"></form>'
    )
    assert forms[0][3] == {"Go": "Go now", "Plain": "Plain"}


def test_a_submit_inside_a_disabled_fieldset_is_not_a_submit():
    forms = _scan_forms(
        '<form action="/cgi-bin/restart.ha"><input name="nonce" value="aa11">'
        '<fieldset disabled><input type="submit" name="Restart" value="Restart"></fieldset></form>'
    )
    assert forms[0][3] == {}


def test_the_page_nonce_ignores_a_commented_out_nonce():
    assert extract_nonce('<!-- <input name="nonce" value="aa11"> --><input name="nonce" value="bb22">') == "bb22"


def test_nonce_selection_stays_fast_with_thousands_of_forms_sharing_one_action():
    import time

    page = "".join(
        f'<form action="/cgi-bin/t.ha"><input name="nonce" value="ab01"><input name="x{i}"></form>'
        for i in range(4000)
    )
    started = time.monotonic()
    _select_write_nonces(page, "t", "t.ha", {f"z{i}": "1" for i in range(4000)})
    assert time.monotonic() - started < 1.0


# --- button-only named actions -----------------------------------------------------------------------


def test_clear_posts_only_its_button_and_nonce_whatever_else_the_page_holds(tmp_env, clock, monkeypatch, capsys):
    page = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="text" name="mode" value="one" />'
        '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
        '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
        '<input type="text" name="mode" value="TWO" /></form>'
    )
    handler = _after_post(page, lambda r: html("", 302, {"location": "/cgi-bin/devices.ha"}), lambda r: html(page))
    _, _, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert len(posts) == 1
    assert parse_qs(posts[0].body.decode()) == {"Clear": ["Clear and Rescan for Devices"], "nonce": ["abc123"]}


def test_restart_from_resets_posts_only_the_live_restart_button(tmp_env, clock, monkeypatch, capsys):
    # The rendered value differs from the catalog's {"Restart": "Restart"}: only the live one is posted.
    page = RESET_FORM.replace('name="Restart" value="Restart"', 'name="Restart" value="Restart Gateway"')
    assert page != RESET_FORM
    handler = _after_post(page, lambda r: html("", 302, {"location": "/cgi-bin/restarting.ha"}))
    code, _, posts, gets_after, _, _ = _cli(
        monkeypatch, capsys, ["action", "restart-from-resets", "--commit", "--confirm", "RESTART"], handler
    )
    assert code == 0 and gets_after == ["/cgi-bin/restarting.ha"]
    assert parse_qs(posts[0].body.decode()) == {"Restart": ["Restart Gateway"], "nonce": ["abc123"]}


def test_generic_submit_still_posts_controls_attached_to_the_form_by_id(tmp_env, clock, monkeypatch, capsys):
    page = (
        '<form id="s" method="post" action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Refresh" value="Refresh" /></form>'
        '<input type="text" form="s" name="protocol" value="tcp" />'
    )
    handler = _after_post(page, lambda r: html("", 302, {"location": "/cgi-bin/etherlan.ha"}), lambda r: html(page))
    argv = ["submit", "etherlan", "Refresh", "--commit", "--confirm", "ETHERLAN"]
    _, _, posts, _, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert len(posts) == 1 and parse_qs(posts[0].body.decode())["protocol"] == ["tcp"]


@pytest.mark.parametrize(
    "reread",
    [
        '<input type="hidden" name="nonce" value="abc123" /><input type="text" name="other" value="x" />',
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Clear" value="Something Else" /></form>',
    ],
    ids=["form-less-reread", "different-value"],
)
def test_the_button_must_be_on_the_page_read_for_the_nonce(tmp_env, clock, monkeypatch, capsys, reread):
    reads = {"n": 0}
    first = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
    )

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/devices.ha"})
        reads["n"] += 1
        return html(first if reads["n"] == 1 else reread)

    code, _, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert code != 0 and posts == []


def test_the_button_is_checked_again_on_the_nonce_read_after_a_login_retry(tmp_env, clock, monkeypatch, capsys):
    login = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
    first = (
        '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
        '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
    )
    state = {"posts": 0, "reads": 0}

    def handler(request, n):
        path = urlsplit(request.url).path
        if path.endswith("login.ha"):
            return html("", 302, {"location": "/cgi-bin/home.ha", "set-cookie": "SessionID=fresh"}) \
                if request.method == "POST" else html(login)
        if request.method == "POST":
            state["posts"] += 1
            return html(login)  # the first POST is answered with the Login page
        state["reads"] += 1
        # The reread keeps an ordinary control, so only the button check (not "no form controls")
        # can refuse the re-send.
        return html(first if state["posts"] == 0 else '<form action="/cgi-bin/devices.ha">'
                    '<input type="hidden" name="nonce" value="abc123" />'
                    '<input type="text" name="mode" value="one" /></form>')

    code, out, _, _, _, captured = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert state["posts"] == 1 and code != 0
    reported = json.dumps(out) + captured.err
    assert "the 'Clear' button" in reported and "re-send was refused" in reported


def test_packet_filter_enable_needs_its_token_again():
    assert get_action("packet-filter-enable").requires_confirmation is True


def test_a_toggle_already_in_the_requested_state_is_unchanged(tmp_env, clock, monkeypatch, capsys):
    page = _pf('<input type="submit" name="Disable" value="Disable Packet Filters" />')
    handler = _after_post(page, lambda r: html("", 302, {"location": "/cgi-bin/packetfilter.ha"}))
    code, out, posts, _, _, _ = _cli(
        monkeypatch, capsys, ["action", "packet-filter-enable", "--commit", "--confirm", "PACKETFILTER"], handler
    )
    assert code == 0 and posts == [] and out["outcome"] == "unchanged" and "already" in out["result"]


# --- one redirect resolver ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "status", "expected"),
    [
        ("/cgi-bin/wifiwarn_advanced.ha", 302, True),
        ("http://router.local:80/cgi-bin/wifiwarn_advanced.ha", 302, True),
        ("/?next=/cgi-bin/wifiwarn_advanced.ha", 302, False),
        ("http://router.local:abc/cgi-bin/wifiwarn_advanced.ha", 302, False),
        ("http://other.example/cgi-bin/wifiwarn_advanced.ha", 302, False),
        ("/cgi-bin/wifiwarn_advanced.ha", 201, False),
    ],
)
def test_the_wifi_warning_gate_resolves_the_location_like_a_browser(location, status, expected):
    assert is_confirmation_redirect(location, status, ORIGIN, "/cgi-bin/wconfig.ha") is expected


def test_an_absolute_same_origin_warning_with_its_default_port_still_gets_continue(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            if "wconfig.ha" in path and "Continue" not in request.body.decode():
                return html("", 302, {"location": "http://router.local:80/cgi-bin/wifiwarn_advanced.ha"})
            return html("", 302, {"location": "/cgi-bin/wconfig.ha"})
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        return html(SCAN_FORM)

    argv = ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"]
    _, _, posts, _, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert len(posts) == 2 and "Continue" in posts[1].body.decode()


@pytest.mark.parametrize(
    ("argv", "source", "location"),
    [
        (["action", "clear-device-list", "--commit"],
         '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
         '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>',
         "http://router.local:bad/cgi-bin/devices.ha"),
        (["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"], SCAN_FORM, "http://[bad"),
        (["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"], SCAN_FORM,
         "http://router.local:bad/cgi-bin/wifiwarn_advanced.ha"),
    ],
    ids=["clear-bad-port", "scan-bad-host", "scan-warning-bad-port"],
)
def test_a_malformed_location_on_any_write_is_no_answer_with_evidence(
    tmp_env, clock, monkeypatch, capsys, argv, source, location
):
    handler = _after_post(source, lambda r: html("", 302, {"location": location}), lambda r: html(source))
    code, out, posts, _, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True


# --- restart: clear outcomes exit 0, ambiguous ones exit 2 ---------------------------------------------


@pytest.mark.parametrize(
    "post_answer",
    [
        lambda r: html('<div id="error-message-text">No changes detected. Save not performed.</div>'),
        lambda r: html("<table><tr><td>" * 400),
        lambda r: html("<title>Restart</title>"),
    ],
    ids=["inline-no-changes", "inline-past-parser-bounds", "inline-plain"],
)
def test_a_restart_answered_inline_is_unconfirmed_with_evidence(tmp_env, clock, monkeypatch, capsys, post_answer):
    code, out, posts, gets_after, _, _ = _cli(monkeypatch, capsys, RESTART, _after_post(RESTART_FORM, post_answer))
    assert code == 2 and len(posts) == 1 and gets_after == []
    assert out["committed"] is False and out["writeAttempted"] is True and out["statusCode"] == 200


@pytest.mark.parametrize(
    "after",
    [
        _raise(TimeoutError("timed out waiting for the status line")),
        lambda r: html("<title>Error</title>", 500),
        lambda r: html("", 503),
    ],
    ids=["timeout-before-headers", "500", "503"],
)
def test_an_ambiguous_hand_off_says_the_restart_may_be_in_progress(tmp_env, clock, monkeypatch, capsys, after):
    handler = _after_post(RESTART_FORM, lambda r: html("", 302, {"location": "/cgi-bin/restarting.ha"}), after)
    code, out, posts, _, _, captured = _cli(monkeypatch, capsys, RESTART, handler)
    assert code == 2 and len(posts) == 1 and out["committed"] is False
    assert UNCERTAIN in out["warning"]


@pytest.mark.parametrize(
    "after",
    [
        _raise(http.client.IncompleteRead(b"partial", 100)),
        _raise(ConnectionResetError("reset")),
        _raise(ConnectionRefusedError("refused")),
        lambda r: html("<title>Restarting</title>"),
    ],
    ids=["cut-mid-answer", "reset", "refused", "restarting-page"],
)
def test_a_clear_hand_off_is_a_requested_restart(tmp_env, clock, monkeypatch, capsys, after):
    handler = _after_post(RESTART_FORM, lambda r: html("", 302, {"location": "/cgi-bin/restarting.ha"}), after)
    code, out, _, _, _, _ = _cli(monkeypatch, capsys, RESTART, handler)
    assert code == 0 and out["committed"] is True


def test_pool_full_on_the_hand_off_names_the_posts_real_status(tmp_env, clock, monkeypatch, capsys):
    handler = _after_post(
        RESTART_FORM, lambda r: html("", 301, {"location": "/cgi-bin/restarting.ha"}),
        lambda r: html("<title>Login</title><p>All web server sessions are in use</p>"),
    )
    code, out, _, _, _, _ = _cli(monkeypatch, capsys, RESTART, handler)
    assert out["sessionPoolFull"] is True and "HTTP 301" in out["error"]


def test_a_hand_off_redirect_whose_query_mentions_login_keeps_the_session(tmp_env, clock, monkeypatch, capsys):
    handler = _after_post(
        RESTART_FORM, lambda r: html("", 302, {"location": "/cgi-bin/restarting.ha"}),
        lambda r: html("", 302, {"location": "/cgi-bin/home.ha?next=login.ha"}),
    )
    code, _, _, _, client, _ = _cli(monkeypatch, capsys, RESTART, handler)
    assert code == 2 and client.has_authenticated_session() is True


# --- docs ---------------------------------------------------------------------------------------------


def test_command_tables_and_help_state_which_actions_need_a_token():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name, text in {
        "README": (root / "README.md").read_text(encoding="utf-8"),
        "REFERENCE": (root / "docs" / "REFERENCE.md").read_text(encoding="utf-8"),
        "root help": cli.help_text(),
    }.items():
        flat = " ".join(text.split())
        assert "Writes: dry-run by default, `--commit --confirm TOKEN` to post" not in flat, name
        assert "Dry-run a guarded router action. Requires `--commit --confirm TOKEN` to POST." not in flat, name
        assert "dry-run a guarded router action; --commit --confirm TOKEN to POST." not in flat, name
