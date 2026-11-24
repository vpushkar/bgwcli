"""--confirm <TOKEN> is required only where a mistake is costly: actions marked dangerous and actions
that take down the gateway's web server or the client's radio. Every other action and the
diagnostics commit with --commit alone. A token given anyway must still be the action's own, so a
mistyped or wrong token never posts."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import ROUTER_ACTIONS, get_action

NONCE_PAGE = '<input name="nonce" value="abc123"><input type="submit" name="Go" value="Go">'
DIAG_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value="">'
    '<textarea name="ProgressWindow">PING example.com: 64 bytes</textarea>'
    '<input type="submit" name="Ping" value="Ping"></form>'
)
TOKEN_FREE = {a.name for a in ROUTER_ACTIONS if not (a.dangerous or a.drops_web_server)}


def _run(monkeypatch, capsys, argv, page=NONCE_PAGE):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/speed.ha"})
        return html(page)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and "/login.ha" not in r.url]
    return code, captured, posts


def test_the_token_is_required_exactly_for_dangerous_and_connection_dropping_actions():
    required = {a.name for a in ROUTER_ACTIONS if a.requires_confirmation}
    assert required == {a.name for a in ROUTER_ACTIONS} - TOKEN_FREE
    assert {"restart", "factory-reset", "restart-wifi-2.4", "restart-wifi-5", "find-best-channel-5"} <= required
    assert {"run-speed-test", "clear-device-list"} <= TOKEN_FREE
    # Enabling filters that hold drop rules can cut the operator off: it keeps its token.
    assert "packet-filter-enable" in required


def test_a_token_free_action_commits_with_commit_alone(tmp_env, clock, monkeypatch, capsys):
    code, captured, posts = _run(monkeypatch, capsys, ["action", "run-speed-test", "--commit"])

    assert code == 0 and len(posts) == 1
    assert json.loads(captured.out)["committed"] is True


def test_a_token_free_action_still_accepts_its_own_token(tmp_env, clock, monkeypatch, capsys):
    code, _, posts = _run(monkeypatch, capsys, ["action", "run-speed-test", "--commit", "--confirm", "SPEED"])

    assert code == 0 and len(posts) == 1


def test_a_wrong_token_on_a_token_free_action_posts_nothing(tmp_env, clock, monkeypatch, capsys):
    code, captured, posts = _run(monkeypatch, capsys, ["action", "run-speed-test", "--commit", "--confirm", "RESTART"])

    assert code != 0 and posts == []
    assert "SPEED" in captured.out + captured.err


@pytest.mark.parametrize("name", ["restart", "factory-reset", "restart-wifi-2.4", "find-best-channel-5"])
def test_commit_alone_is_refused_where_the_token_is_required(tmp_env, clock, monkeypatch, capsys, name):
    code, captured, posts = _run(monkeypatch, capsys, ["action", name, "--commit"])

    assert code != 0 and posts == []
    assert f"--confirm {get_action(name).confirm_token}" in captured.out + captured.err


def test_diagnostics_commit_with_commit_alone(tmp_env, clock, monkeypatch, capsys):
    code, _, posts = _run(monkeypatch, capsys, ["diagnostics", "ping", "example.com", "--commit"], page=DIAG_FORM)

    assert len(posts) == 1 and code == 0


def test_diagnostics_refuse_a_wrong_token(tmp_env, clock, monkeypatch, capsys):
    code, _, posts = _run(
        monkeypatch, capsys, ["diagnostics", "ping", "example.com", "--commit", "--confirm", "RESTART"], page=DIAG_FORM
    )

    assert code != 0 and posts == []


@pytest.mark.parametrize(
    ("argv", "commit_command"),
    [
        (["action", "run-speed-test"], "action run-speed-test --commit"),
        (["action", "restart"], "action restart --commit --confirm RESTART"),
        (["diagnostics", "ping", "example.com"], "diagnostics ping example.com --commit"),
    ],
)
def test_the_dry_run_prints_the_commit_command_with_a_token_only_where_required(
    tmp_env, clock, monkeypatch, capsys, argv, commit_command
):
    code, captured, posts = _run(monkeypatch, capsys, argv, page=DIAG_FORM if argv[0] == "diagnostics" else NONCE_PAGE)

    assert code == 0 and posts == []
    assert json.loads(captured.out)["commitCommand"] == commit_command
