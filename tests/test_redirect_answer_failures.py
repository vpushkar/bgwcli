"""The page a write's 302 redirects to carries the gateway's answer (a rejection banner). When reading
it fails with an HTTP error, a refused session or a full session pool, the write's answer is unknown:
a failed step (exit 2) or a raised session/pool failure carrying the write evidence, never a success.
Only a lost connection on that optional read leaves the POST's own answer standing."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli, session
from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterConnectionError

LOGIN_403 = '<title>Login</title><form><input name="nonce" value="n"><input name="password"></form>'


def _redirect_handler(page, failure, *, base=None):
    state = {"posted": False}

    def handler(request, _n):
        if request.url.endswith("/login.ha"):
            if request.method == "POST":
                return html("<html><body>Login Failed</body></html>")
            return html(LOGIN_403)
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": f"/cgi-bin/{page}.ha"})
        if state["posted"]:
            if failure == "pool":
                raise session_pool_full_error(waited_ms=2300, retry_count=4)
            if failure == "auth":
                return html(LOGIN_403, 403)
            if failure == "connection":
                raise RouterConnectionError("synthetic connection reset")
            return html("<title>Unavailable</title>", 503)
        return html(base or form(page, "old"))
    return handler


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests)
    cooldown = session.read_session_state(client.session_identity()).pool_cooldown_until
    return code, out, posts, cooldown


@pytest.mark.parametrize("failure", ["http", "auth", "pool"])
def test_action_redirect_read_failure_is_no_answer(tmp_env, clock, monkeypatch, capsys, failure):
    code, out, posts, cooldown = _run(
        monkeypatch, capsys, _redirect_handler("speed", failure),
        ["action", "run-speed-test", "--commit", "--confirm", "SPEED"],
    )
    assert code == 2 and posts == 1
    assert out.get("committed") is not True
    assert out["writeAttempted"] is True
    if failure == "pool":
        assert out["sessionPoolFull"] is True and cooldown is not None
    if failure == "http":
        # The POST's own answer (302) stays the reported status; the redirect read's 503 is the error.
        assert out["statusCode"] == 302 and "503" in out["warning"]


def test_action_redirect_connection_loss_is_no_answer(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _ = _run(
        monkeypatch, capsys, _redirect_handler("speed", "connection"),
        ["action", "run-speed-test", "--commit", "--confirm", "SPEED"],
    )
    assert (code, posts, out["committed"]) == (2, 1, False)
    assert out["writeAttempted"] is True and out["statusCode"] == 302


def test_saveless_submit_redirect_pool_failure_raises_with_cooldown(tmp_env, clock, monkeypatch, capsys):
    base = form("diag", "old").replace('name="Save" value="Save"', 'name="Ping" value="Ping"')
    code, out, posts, cooldown = _run(
        monkeypatch, capsys, _redirect_handler("diag", "pool", base=base),
        ["submit", "diag", "Ping", "--commit", "--confirm", "DIAG"],
    )
    assert (code, posts) == (2, 1)
    assert out["sessionPoolFull"] is True and out["writeAttempted"] is True and cooldown is not None


def test_diagnostic_redirect_http_failure_is_no_answer(tmp_env, clock, monkeypatch, capsys):
    diag = ('<form action="/cgi-bin/diag.ha"><input name="nonce" value="ab12"><input name="WebAddress" value="">'
            '<input type="submit" name="Ping" value="Ping"></form>')
    code, out, posts, _ = _run(
        monkeypatch, capsys, _redirect_handler("diag", "http", base=diag),
        ["diagnostics", "ping", "example.invalid", "--commit", "--confirm", "DIAG"],
    )
    assert (code, posts) == (2, 1)
    assert out["committed"] is False and out["writeAttempted"] is True and "503" in out["warning"]


DIAG = ('<title>Diagnostics</title><form method="post" action="/cgi-bin/diag.ha">'
        '<input name="nonce" value="abc123"><input name="WebAddress" value="">'
        '<input type="submit" name="Ping" value="Ping"></form>')
PING_OUTPUT = "1 packets transmitted, 1 received, 0% packet loss"


def test_diagnostic_result_on_the_redirect_target_is_kept(tmp_env, clock, monkeypatch, capsys):
    """The page read for the rejection banner after the 302 already carries the result: it is the
    poll's starting state, so a window the gateway clears afterwards still reports that output."""
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda _s: None)
    state = {"posted": False, "reads": 0}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/diag.ha"})
        if state["posted"]:
            state["reads"] += 1
            text = PING_OUTPUT if state["reads"] == 1 else ""
            return html(DIAG + '<textarea name="ProgressWindow">' + text + "</textarea>")
        return html(DIAG)

    code, out, posts, _ = _run(monkeypatch, capsys, handler,
                               ["diagnostics", "ping", "example.invalid", "--commit", "--confirm", "DIAG"])
    assert (code, posts) == (0, 1)
    assert PING_OUTPUT in out["result"]
    assert state["reads"] == 2  # the banner read plus one poll that sees the window cleared


@pytest.mark.parametrize("json_mode", [True, False])
def test_diagnostic_without_output_by_the_deadline_is_started_but_not_yet_available(
    tmp_env, clock, monkeypatch, capsys, json_mode
):
    """A diagnostic the gateway accepted is a successful start (exit 0) even when no output appeared
    within the poll window; the result says plainly that it is not available yet."""
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda _s: None)
    state = {"posted": False}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/diag.ha"})
        return html(DIAG + '<textarea name="ProgressWindow"></textarea>')

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    argv = ["diagnostics", "ping", "example.invalid", "--commit", "--confirm", "DIAG"]
    code = cli.main([*argv, "--json"] if json_mode else argv)
    out = capsys.readouterr().out
    assert code == 0 and sum(r.method == "POST" for r in wire.requests) == 1
    if json_mode:
        payload = json.loads(out)
        assert payload["committed"] is True and payload["resultAvailable"] is False
        assert "not yet available" in payload["result"]
    else:
        assert "not yet available" in out


def test_diagnostic_with_output_says_the_result_is_available(tmp_env, clock, monkeypatch, capsys):
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda _s: None)

    def handler(request, _n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/diag.ha"})
        return html(DIAG + '<textarea name="ProgressWindow">' + PING_OUTPUT + "</textarea>")

    code, out, posts, _ = _run(monkeypatch, capsys, handler,
                               ["diagnostics", "ping", "example.invalid", "--commit", "--confirm", "DIAG"])
    assert (code, posts, out["resultAvailable"]) == (0, 1, True)
