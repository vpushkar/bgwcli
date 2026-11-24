"""Final-review fixes (Codex, third review of the live-button / hand-off / token series)."""

import json
import socket
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.client import NoncePageRefusedError, _select_write_nonces

NONCE_PAGE = '<input name="nonce" value="abc123"><input type="submit" name="Go" value="Go">'
DIAG_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value=""><input type="submit" name="Ping" value="Ping"></form>'
)
RESTART_FORM = (
    '<form method="post" action="/cgi-bin/restart.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Restart" value="Restart" /><input type="submit" name="Cancel" value="Cancel" /></form>'
)
DEVICES_FORM = (
    '<form method="post" action="/cgi-bin/devices.ha"><input type="hidden" name="nonce" value="abc123" />'
    '<input type="submit" name="Clear" value="Clear and Rescan for Devices" /></form>'
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


def _pf_page(toggle: str, rule_value: str = "") -> str:
    return (
        '<form method="post" action="/cgi-bin/packetfilter.ha"><input type="hidden" name="nonce" value="abc123" />'
        f"{toggle}"
        f'<input type="submit" name="Complete_16" class="checkbutton" value="{rule_value}"'
        ' title="Enable/Disable Rule" />'
        "</form>"
    )


PF_ENABLED = _pf_page('<input type="submit" name="Disable" value="Disable Packet Filters" />')
PF_DISABLED = _pf_page('<input type="submit" name="Enable" value="Enable Packet Filters" />')


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


def _source_then(source, post_answer, after_post=lambda r: html("<title>Restarting</title>")):
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return post_answer(request)
        if state["posted"]:
            return after_post(request)
        return html(source)

    return handler


# --- an empty --confirm= never authorizes a POST ---------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "page"),
    [
        (["action", "run-speed-test", "--commit", "--confirm="], NONCE_PAGE),
        (["diagnostics", "ping", "example.com", "--commit", "--confirm="], DIAG_FORM),
    ],
)
def test_an_empty_confirm_value_posts_nothing(tmp_env, clock, monkeypatch, capsys, argv, page):
    handler = _source_then(page, lambda r: html("", 302, {"location": "/cgi-bin/speed.ha"}))
    code, _, posts, _, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert code != 0 and posts == []


# --- a malformed redirect on a write is an unreadable answer ---------------------------------------


def test_a_malformed_redirect_after_clear_is_no_answer_with_write_evidence(tmp_env, clock, monkeypatch, capsys):
    handler = _source_then(DEVICES_FORM, lambda r: html("", 302, {"location": "http://[bad"}))
    code, out, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True and out["statusCode"] == 302


# --- restart: only a GET redirect to restarting.ha, reached on the wire, is a requested restart -----


@pytest.mark.parametrize(
    "post_answer",
    [
        lambda r: html("", 307, {"location": "/cgi-bin/restarting.ha"}),
        lambda r: html("", 308, {"location": "http://other.example/cgi-bin/restarting.ha"}),
        lambda r: html("<title>Restart</title>"),
    ],
    ids=["307", "308-off-origin", "200-no-redirect"],
)
def test_restart_without_a_get_redirect_to_the_restarting_page_is_unconfirmed(
    tmp_env, clock, monkeypatch, capsys, post_answer
):
    handler = _source_then(RESTART_FORM, post_answer)
    argv = ["action", "restart", "--commit", "--confirm", "RESTART"]
    code, out, posts, gets_after, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert code == 2 and len(posts) == 1 and gets_after == []
    assert out["committed"] is False and out["writeAttempted"] is True


def _restart(monkeypatch, capsys, after_post, location="/cgi-bin/restarting.ha"):
    handler = _source_then(RESTART_FORM, lambda r: html("", 302, {"location": location}), after_post)
    return _cli(monkeypatch, capsys, ["action", "restart", "--commit", "--confirm", "RESTART"], handler)


def _raise(exc):
    def answer(request):
        raise exc

    return answer


def test_a_name_resolution_failure_before_the_hand_off_is_unconfirmed(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _, _, _ = _restart(monkeypatch, capsys, _raise(socket.gaierror(8, "nodename nor servname")))
    assert code == 2 and out["committed"] is False and out["writeAttempted"] is True


@pytest.mark.parametrize("exc", [ConnectionResetError("reset by peer"), ConnectionRefusedError("refused")])
def test_the_gateway_dropping_or_refusing_the_hand_off_is_a_requested_unverified_restart(
    tmp_env, clock, monkeypatch, capsys, exc
):
    code, out, posts, _, _, _ = _restart(monkeypatch, capsys, _raise(exc))
    assert code == 0 and out["committed"] is True and "not verified" in out["result"]


@pytest.mark.parametrize(
    "body",
    [
        "<title>Page not found</title><p>Page not found</p>",
        '<div id="error-message-text">No changes detected. Save not performed.</div>',
        "<table><tr><td>" * 400,
    ],
    ids=["page-not-found", "no-changes", "past-parser-bounds"],
)
def test_a_restarting_page_that_is_not_a_restart_is_unconfirmed_with_evidence(
    tmp_env, clock, monkeypatch, capsys, body
):
    code, out, posts, _, _, _ = _restart(monkeypatch, capsys, lambda r: html(body))
    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True and out["statusCode"] == 302


@pytest.mark.parametrize(
    "answer",
    [lambda r: html('<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'),
     lambda r: html("<title>Forbidden</title>", 403)],
    ids=["login", "403"],
)
def test_a_lost_session_on_the_hand_off_is_forgotten(tmp_env, clock, monkeypatch, capsys, answer):
    code, out, _, _, client, _ = _restart(monkeypatch, capsys, answer)
    assert code == 2 and client.has_authenticated_session() is False


def test_a_full_pool_on_the_hand_off_raises_with_the_write_evidence(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _, _, _ = _restart(
        monkeypatch, capsys, lambda r: html("<title>Login</title><p>All web server sessions are in use</p>")
    )
    assert code != 0 and len(posts) == 1
    assert out["sessionPoolFull"] is True and out["writeAttempted"] is True


@pytest.mark.parametrize(
    "location", ["http://router.local:0/cgi-bin/restarting.ha", "/cgi-bin/restarting.ha/", "/cgi-bin/restarting.ha%2F"]
)
def test_the_restarting_page_url_must_match_exactly(tmp_env, clock, monkeypatch, capsys, location):
    code, out, posts, gets_after, _, _ = _restart(monkeypatch, capsys, lambda r: html("ok"), location=location)
    assert code == 2 and gets_after == []


# --- the Wi-Fi Warning Continue needs a same-origin GET redirect ------------------------------------


@pytest.mark.parametrize(
    "post_answer",
    [
        lambda r: html("", 201, {"location": "/cgi-bin/wifiwarn_advanced.ha"}),
        lambda r: html("", 302, {"location": "http://other.example/cgi-bin/wifiwarn_advanced.ha"}),
    ],
    ids=["201", "302-off-origin"],
)
def test_the_wifi_warning_is_followed_only_for_a_same_origin_get_redirect(
    tmp_env, clock, monkeypatch, capsys, post_answer
):
    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            return post_answer(request) if "wconfig.ha" in path else html("", 302, {"location": "/cgi-bin/wconfig.ha"})
        if "wifiwarn" in path:
            return html(WARNING_PAGE)
        return html(SCAN_FORM)

    argv = ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"]
    code, out, posts, _, _, _ = _cli(monkeypatch, capsys, argv, handler)
    assert len(posts) == 1


# --- named actions post their exact submit name, from their own form, as read at post time ----------


def test_packet_filter_enable_never_posts_a_rule_button_whose_value_is_enable(tmp_env, clock, monkeypatch, capsys):
    handler = _source_then(_pf_page('<input type="submit" name="Disable" value="Disable Packet Filters" />', "Enable"),
                           lambda r: html("", 302, {"location": "/cgi-bin/packetfilter.ha"}))
    code, _, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "packet-filter-enable", "--commit"], handler)
    assert code != 0 and posts == []


def test_a_named_action_posts_only_its_owning_forms_fields(tmp_env, clock, monkeypatch, capsys):
    page = DEVICES_FORM + (
        '<form method="post" action="/cgi-bin/other.ha"><input type="hidden" name="nonce" value="ff00" />'
        '<input type="text" name="mode" value="manual" /></form>'
    )
    handler = _source_then(page, lambda r: html("", 302, {"location": "/cgi-bin/devices.ha"}),
                           lambda r: html(page))
    code, _, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "clear-device-list", "--commit"], handler)
    assert len(posts) == 1
    assert parse_qs(posts[0].body.decode()) == {"Clear": ["Clear and Rescan for Devices"], "nonce": ["abc123"]}


def test_a_button_gone_from_the_page_read_for_the_nonce_posts_nothing(tmp_env, clock, monkeypatch, capsys):
    reads = {"n": 0}

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/packetfilter.ha"})
        reads["n"] += 1
        return html(PF_DISABLED if reads["n"] == 1 else PF_ENABLED)

    code, _, posts, _, _, _ = _cli(monkeypatch, capsys, ["action", "packet-filter-enable", "--commit"], handler)
    assert code != 0 and posts == []


# --- the form scanner reads markup the way a browser tokenizes it ------------------------------------


@pytest.mark.parametrize(
    "decoy",
    [
        '<textarea name="help"><form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="ee01"></textarea>',
        '<title><form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="ee01"></title>',
        '<input type="hidden" name="tip" '
        """value='<form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="ee01">'>""",
    ],
    ids=["textarea", "title", "attribute"],
)
def test_form_markup_inside_text_or_an_attribute_is_not_a_form(decoy):
    page = (
        f"{decoy}"
        '<form method="post" action="/cgi-bin/wrestart.ha?2"><input type="hidden" name="nonce" value="ee02" />'
        '<input type="submit" name="WRestart2" value="Restart" /></form>'
    )
    with pytest.raises(NoncePageRefusedError):
        _select_write_nonces(page, "home", "wrestart.ha?1", {"WRestart1": "Restart"})
    assert _select_write_nonces(page, "home", "wrestart.ha?2", {"WRestart2": "Restart"}) == ["ee02"]


def test_nonce_selection_stays_fast_with_thousands_of_forms():
    forms = "".join(
        f'<form action="/cgi-bin/f{i}.ha"><input name="nonce" value="ab{i:04x}"><input name="x{i}"></form>'
        for i in range(8000)
    )
    page = forms + '<form action="/cgi-bin/t.ha"><input name="nonce" value="cd01"><input name="y"></form>'
    fields = {f"z{i}": "1" for i in range(8000)} | {"y": "1"}
    started = time.monotonic()
    assert _select_write_nonces(page, "t", "t.ha", fields) == ["cd01"]
    assert time.monotonic() - started < 1.0


# --- docs state the token rule the code enforces ---------------------------------------------------


def test_help_and_docs_no_longer_say_every_write_needs_a_token():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    texts = {
        "root help": cli.help_text(),
        "README": (root / "README.md").read_text(encoding="utf-8"),
        "REFERENCE": (root / "docs" / "REFERENCE.md").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        flat = " ".join(text.split())
        assert "Required token for every committed operation" not in flat, name
        assert "Every write is dry-run by default and needs `--commit --confirm TOKEN`" not in flat, name
        assert "Actual action POSTs require `--commit --confirm TOKEN`" not in flat, name
