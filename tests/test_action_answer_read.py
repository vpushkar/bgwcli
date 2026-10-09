"""Actions whose effect drops the gateway's web server never read the redirect answer; every other
action whose answer cannot be read is "no answer"."""

import json

import pytest
from save_helpers import ERROR, client_with, form, html

from bgwcli import cli
from bgwcli.actions import ROUTER_ACTIONS, get_action
from bgwcli.errors import RouterConnectionError

DROPPING = ("restart", "restart-from-resets", "reset-ip", "reset-connection", "reset-wifi-config",
            "reset-firewall-config", "factory-reset", "restart-broadband",
            "restart-wifi-2.4", "restart-wifi-5", "find-best-channel-5")
SCAN_FORM = (
    '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old">'
    '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
)


def _run(monkeypatch, capsys, name, handler):
    action = get_action(name)
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["action", name, "--commit", "--confirm", action.confirm_token, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, out, posts, wire


def _handler(action, answer):
    state = {"posted": False, "reads_after_post": 0}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": f"/cgi-bin/{action.page}.ha"})
        if state["posted"]:
            state["reads_after_post"] += 1
            return answer()
        if action.post_path:  # the page's form posts to the action's own CGI (home's Restart forms)
            return html(
                f'<form action="/cgi-bin/{action.post_path}"><input name="nonce" value="abc123">'
                '<input type="submit" name="Restart" value="Restart"></form>'
            )
        return html(SCAN_FORM if action.form_button else form(action.page, "old"))

    handler.state = state
    return handler


def test_the_dropping_set_is_exactly_the_restart_and_reset_family():
    assert {a.name for a in ROUTER_ACTIONS if a.drops_web_server} == set(DROPPING)


@pytest.mark.parametrize("name", DROPPING)
@pytest.mark.parametrize("answer_kind", ["connection", "http"])
def test_dropping_action_reports_committed_without_reading_the_answer(
    tmp_env, clock, monkeypatch, capsys, name, answer_kind
):
    def unreadable():
        if answer_kind == "connection":
            raise RouterConnectionError("connection reset")
        return html("<title>Unavailable</title>", 503)

    handler = _handler(get_action(name), unreadable)
    code, out, posts, _ = _run(monkeypatch, capsys, name, handler)
    assert code == 0 and len(posts) == 1
    assert out["committed"] is True and out["answerRead"] is False
    assert out["statusCode"] == 302 and out["writeAttempted"] is True
    assert handler.state["reads_after_post"] == 0


def test_dropping_action_still_honours_an_inline_rejection(tmp_env, clock, monkeypatch, capsys):
    action = get_action("restart")

    def handler(request, n):
        if request.method == "POST":
            return html(form(action.page, "old", banner=ERROR))
        return html(form(action.page, "old"))

    code, out, posts, _ = _run(monkeypatch, capsys, "restart", handler)
    assert code == 1 and len(posts) == 1 and out["committed"] is False


@pytest.mark.parametrize("name", ["run-speed-test", "clear-device-list"])
@pytest.mark.parametrize("answer_kind", ["connection", "http"])
def test_other_actions_with_an_unreadable_answer_are_no_answer(
    tmp_env, clock, monkeypatch, capsys, name, answer_kind
):
    def unreadable():
        if answer_kind == "connection":
            raise RouterConnectionError("connection reset")
        return html("<title>Unavailable</title>", 503)

    code, out, posts, _ = _run(monkeypatch, capsys, name, _handler(get_action(name), unreadable))
    assert code == 2 and len(posts) == 1
    assert out["committed"] is False and out["writeAttempted"] is True
    assert "answerRead" not in out


def test_an_ordinary_action_with_a_readable_answer_carries_no_answer_read_flag(tmp_env, clock, monkeypatch, capsys):
    action = get_action("run-speed-test")
    handler = _handler(action, lambda: html(form(action.page, "old")))
    code, out, posts, _ = _run(monkeypatch, capsys, "run-speed-test", handler)
    assert code == 0 and out["committed"] is True and "answerRead" not in out


def test_form_button_action_with_a_bannerless_follow_up_page_is_committed_without_acknowledgement(
    tmp_env, clock, monkeypatch, capsys
):
    scan = (
        '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
        '<input type="text" name="setting" value="old">'
        '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
    )

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/wconfig.ha"})
        return html(scan)

    code, out, posts, _ = _run(monkeypatch, capsys, "find-best-channel-5", handler)
    assert code == 0 and len(posts) == 1 and out["committed"] is True
    assert not out.get("acknowledgementObserved")
