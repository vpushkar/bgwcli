"""A save's authentication failure and the cached router session, end to end.

Runs `cli.main(["set", ..., "--json"])` over the real client, an in-memory transport and the real
session coordinator with a cached session already on disk. A 403 that the saved page itself answers
says nothing about the session, so the cached session survives; a 403 carrying the Login page (or
answered by login.ha after a redirect) means the gateway dropped the session, so the cache goes.
"""

from __future__ import annotations

import json

import pytest
from save_helpers import SAVED_RED, FakeTransport, form, html

from bgwcli import cli, session
from bgwcli.client import BGW320Client

ORIGIN = "http://router.local"
LOGIN_PAGE = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
SET_ARGV = ["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN", "--host", ORIGIN, "--json"]


def _cached_session():
    paths = session.session_paths(ORIGIN)
    session._ensure_private_dir(paths.cache.parent)
    now = session._now_ms()
    session._write_json(paths.cache, {
        "origin": ORIGIN, "authenticated": True, "cookies": {"sid": "cached"}, "version": 1,
        "cachedAt": now, "expiresAt": now + 600_000,
    })
    return paths


def _run(monkeypatch, capsys, handle):
    transport = FakeTransport(handle)
    client = BGW320Client(ORIGIN, access_code="12345", timeout_ms=1000, user_agent="test", transport=transport)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(SET_ARGV)
    captured = capsys.readouterr()
    return code, captured, transport


def test_page_level_403_on_the_save_post_keeps_the_cached_session(clock, tmp_env, capsys, monkeypatch):
    paths = _cached_session()

    def handle(request, n):
        if request.method == "POST":
            # The page itself refuses the write: no Login body, the URL is the page.
            return html("<html><body>Forbidden</body></html>", status=403)
        return html(form("etherlan", "old"))

    code, captured, transport = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    output = json.loads(captured.out)
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output["statusCode"] == 403
    assert paths.cache.exists(), "a page-level 403 says nothing about the session"
    assert json.loads(paths.cache.read_text())["cookies"] == {"sid": "cached"}
    assert not any("login.ha" in call for call in transport.calls), "the cached session was used, no login"


@pytest.mark.parametrize("shape", ["login-body-on-post", "nonce-read-redirected-to-login"])
def test_session_wide_403_on_the_save_drops_the_cached_session(clock, tmp_env, capsys, monkeypatch, shape):
    paths = _cached_session()

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        if path.startswith("login.ha"):
            if shape == "nonce-read-redirected-to-login":
                return html("<html><body>Forbidden</body></html>", status=403)
            if request.method == "POST":
                return html("<html><body>Login Failed</body></html>")
            return html(LOGIN_PAGE)
        if request.method == "POST":
            return html(LOGIN_PAGE, status=403)
        if shape == "nonce-read-redirected-to-login" and n == 2:
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(form("etherlan", "old"))

    code, captured, _ = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert not paths.cache.exists(), "the gateway dropped the session; the cached copy is dead"


def test_reread_bounced_to_login_with_a_refused_relogin_forgets_the_cached_session(clock, tmp_env, capsys, monkeypatch):
    """The save is acknowledged, then the verification re-read is bounced to login.ha and the
    gateway refuses the client's re-login. The command ends logged out: the cached session is dead
    and is forgotten, and the warning says the session was lost and the re-login refused."""
    monkeypatch.setattr(cli, "sleep", lambda seconds: None, raising=False)
    paths = _cached_session()
    state = {"posted": False}

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        if path.startswith("login.ha") or path.startswith("services.ha"):
            return html(LOGIN_PAGE)  # every login is refused: the Login page again
        if request.method == "POST":
            state["posted"] = True
            return html(form("etherlan", "new", banner=SAVED_RED))
        if state["posted"]:
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(form("etherlan", "old"))

    code, captured, transport = _run(monkeypatch, capsys, handle)

    assert code == 2
    output = json.loads(captured.out)
    assert output["committed"] is True and output["outcome"] == "applied" and "verified" not in output
    assert "session was lost" in output["warning"] and "re-login was refused" in output["warning"]
    assert not paths.cache.exists(), "the command ended logged out; the cached session is dead"
    assert sum(r.method == "POST" and not r.url.endswith("/login.ha") for r in transport.requests) == 1
