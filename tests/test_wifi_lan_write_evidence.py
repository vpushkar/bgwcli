"""Wi-Fi and LAN saves report the same retried-write and acknowledged-write evidence as every other
write path, on success, on HTTP faults and on pool exhaustion."""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli, session
from bgwcli.client import session_pool_full_error

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha")]
    return code, out, posts, client


@pytest.mark.parametrize(
    "page,name,old,new", [("wconfig", "setting", "old", "new"), ("dhcpserver", "dhcp", "on", "off")]
)
@pytest.mark.parametrize("failure", ["retry_nonce_500", "retry_post_timeout"])
def test_login_body_retry_reports_each_post_truthfully(
    tmp_env, clock, monkeypatch, capsys, page, name, old, new, failure
):
    state = {"posts": 0, "logged_in": False}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            if request.method == "POST":
                state["logged_in"] = True
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 1:
                return html(LOGIN)
            raise TimeoutError("second configuration POST lost")
        if state["logged_in"] and failure == "retry_nonce_500":
            return html("<title>Server Error</title>", 500)
        return html(form(page, old, name=name))

    code, out, posts, _ = _run(monkeypatch, capsys, handler,
                               ["set", page, f"{name}={new}", "--commit", "--confirm", page.upper()])
    assert code == 2 and out["writeAttempted"] is True
    if failure == "retry_nonce_500":
        assert len(posts) == 1 and out["writeAttempts"] == 1
        assert out["statusCode"] == 200 and out["writeResponseReceived"] is True
        assert "500" in out["warning"]
    else:
        assert len(posts) == 2 and out["writeAttempts"] == 2
        assert out["writeResponseReceived"] is False and out.get("statusCode") is None
        assert "sent once" not in out["warning"] and "2 times" in out["warning"]


def _lan_handler(fault):
    state = {"posted": False, "reads": 0}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/dhcpserver.ha"})
        if state["posted"]:
            state["reads"] += 1
            if state["reads"] > 1:
                if fault == "http":
                    return html("<title>Unavailable</title>", 503)
                raise session_pool_full_error(waited_ms=2300, retry_count=4)
        return html(form("dhcpserver", "on", name="dhcp", banner=SAVED_RED if state["posted"] else ""))

    return handler


def test_lan_acknowledgement_then_http_fault_keeps_the_commit(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, _ = _run(monkeypatch, capsys, _lan_handler("http"),
                               ["set", "dhcpserver", "dhcp=off", "--commit", "--confirm", "DHCPSERVER"])
    assert code == 2 and len(posts) == 1
    assert out["acknowledgementObserved"] is True and out["writePerformed"] is True
    assert out["committed"] is True


def test_lan_acknowledgement_then_pool_full_keeps_the_evidence(tmp_env, clock, monkeypatch, capsys):
    code, out, posts, client = _run(monkeypatch, capsys, _lan_handler("pool"),
                                    ["set", "dhcpserver", "dhcp=off", "--commit", "--confirm", "DHCPSERVER"])
    assert code == 2 and len(posts) == 1 and out["sessionPoolFull"] is True
    assert out["writeAttempted"] is True
    assert out["acknowledgementObserved"] is True and out["writePerformed"] is True
    assert out["committed"] is True
    assert session.read_session_state(client.session_identity()).pool_cooldown_until is not None
