"""A configuration POST answered with the Login page is re-sent once after a forced login. The
reported write evidence belongs to the POSTs actually sent: a later GET's HTTP error is never the
POST's status, a lost second POST is not "answered", and the attempt count is reported."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'


@pytest.mark.parametrize("failure", ["retry_nonce_500", "retry_post_timeout"])
def test_login_body_retry_reports_each_post_truthfully(tmp_env, clock, monkeypatch, capsys, failure):
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
        return html(form("etherlan", "old"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main(["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN", "--json"])
    out = json.loads(capsys.readouterr().out)
    config_posts = [r for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha")]
    assert code == 2 and out["writeAttempted"] is True
    if failure == "retry_nonce_500":
        # The only configuration POST answered 200 (the Login page); the 500 came from the retry's GET.
        assert len(config_posts) == 1 and out["writeAttempts"] == 1
        assert out["statusCode"] == 200 and out["writeResponseReceived"] is True
        assert "500" in out["warning"]
    else:
        # Two POSTs went out; the last one has no answer, so delivery of the change is unknown.
        assert len(config_posts) == 2 and out["writeAttempts"] == 2
        assert out["writeResponseReceived"] is False and out.get("statusCode") is None
        assert "sent once" not in out["warning"] and "2 times" in out["warning"]
