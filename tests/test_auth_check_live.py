"""`auth` verifies the access code against the gateway and `check` reports a verified session only.

Drives `cli.main` over the real client with an in-memory transport whose session cookie was
imported as already authenticated (what a cached session looks like to the command).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from save_helpers import client_with, html

from bgwcli import cli, session

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
SITEMAP = "<title>Site Map</title><a href='/cgi-bin/home.ha'>Status</a>"


def _run(monkeypatch, capsys, argv, handle):
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured, wire


def _login_posts(wire):
    return [r for r in wire.requests if r.method == "POST" and urlsplit(r.url).path == "/cgi-bin/login.ha"]


def test_auth_logs_in_even_when_a_session_is_already_held(tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(LOGIN)

    code, captured, wire = _run(monkeypatch, capsys, ["auth", "--json"], handle)

    assert code == 0 and json.loads(captured.out) == {"authenticated": True}
    posts = _login_posts(wire)
    assert len(posts) == 1, "the access code must be verified by a real login"
    assert "hashpassword" in parse_qs(posts[0].body.decode())


def test_auth_with_a_rejected_code_fails_even_when_a_session_is_held(tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if request.method == "POST":
            return html("<title>Login</title><p>Login Failed</p>" + LOGIN)
        return html(LOGIN)

    code, captured, wire = _run(monkeypatch, capsys, ["auth"], handle)

    assert code == 2
    assert "Login failed" in captured.err and captured.out == ""
    assert len(_login_posts(wire)) == 1


def test_check_reports_a_dead_cached_session_as_not_authenticated(tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if urlsplit(request.url).path == "/cgi-bin/sitemap.ha":
            return html(SITEMAP)
        return html(LOGIN)  # every protected page answers the Login page: the cookie is dead

    code, captured, wire = _run(monkeypatch, capsys, ["check", "--json"], handle)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["reachable"] is True and payload["authenticated"] is False
    assert not any(r.method == "POST" for r in wire.requests), "check never logs in"


def test_check_reports_a_live_session_as_authenticated(tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if urlsplit(request.url).path == "/cgi-bin/sitemap.ha":
            return html(SITEMAP)
        return html("<title>Services</title><table><tr><td>a</td></tr></table>")

    code, captured, wire = _run(monkeypatch, capsys, ["check", "--json"], handle)

    assert code == 0 and json.loads(captured.out)["authenticated"] is True
    assert not any(r.method == "POST" for r in wire.requests)


import pytest  # noqa: E402 - grouped with the tests that use it


@pytest.mark.parametrize("command", ["sitemap", "coverage"])
@pytest.mark.parametrize("status", [404, 500, 403])
def test_sitemap_http_error_is_no_answer(tmp_env, capsys, monkeypatch, command, status):
    def handle(request, n):
        return html("<html>Error</html>", status=status)

    code, captured, wire = _run(monkeypatch, capsys, [command, "--json"], handle)

    assert code == 2
    payload = json.loads(captured.out)
    assert payload["ok"] is False and f"HTTP {status}" in payload["error"]
    assert not any(r.method == "POST" for r in wire.requests)


@pytest.mark.parametrize("command", ["sitemap", "coverage"])
def test_sitemap_http_error_text_mode_exits_2(tmp_env, capsys, monkeypatch, command):
    code, captured, _ = _run(monkeypatch, capsys, [command], lambda request, n: html("x", status=500))
    assert code == 2 and "HTTP 500" in (captured.out + captured.err)


def test_check_reports_a_full_session_pool_instead_of_an_unauthenticated_session(tmp_env, capsys, monkeypatch):
    from bgwcli import session

    full = "<title>Login</title><p>All web server sessions are in use</p>"

    def handle(request, n):
        if urlsplit(request.url).path == "/cgi-bin/sitemap.ha":
            return html(SITEMAP)
        return html(full)

    code, captured, wire = _run(monkeypatch, capsys, ["check", "--host", "http://router.local", "--json"], handle)

    assert code == 2
    payload = json.loads(captured.out)
    assert payload["sessionPoolFull"] is True and "authenticated" not in payload
    assert session.session_paths("http://router.local").cooldown.exists()
    assert not any(r.method == "POST" for r in wire.requests), "check never logs in"


def test_check_with_wait_for_session_waits_out_a_full_pool_then_reports_the_session(tmp_env, capsys, monkeypatch):
    from save_helpers import FakeTransport

    import bgwcli.client as client_module
    from bgwcli.client import BGW320Client

    full = "<title>Login</title><p>All web server sessions are in use</p>"
    state = {"login_polls": 0}

    def handle(request, n):
        path = urlsplit(request.url).path
        if path == "/cgi-bin/sitemap.ha":
            return html(SITEMAP)
        if path == "/cgi-bin/login.ha":
            state["login_polls"] += 1
            return html(full if state["login_polls"] < 2 else LOGIN)
        if state["login_polls"] < 2:
            return html(full)
        return html("<title>Services</title><table><tr><td>a</td></tr></table>")

    transport = FakeTransport(handle)

    def factory(options, access_code, **kwargs):
        client = BGW320Client.from_options(options, access_code, transport=transport, **kwargs)
        client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: None)
    argv = ["check", "--host", "http://router.local", "--wait-for-session", "--session-wait-interval", "1", "--json"]
    code = cli.main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["authenticated"] is True
    assert state["login_polls"] == 2
    assert not any(r.method == "POST" for r in transport.requests), "check never logs in"


def test_check_with_wait_for_session_still_reports_a_pool_that_stays_full(tmp_env, capsys, monkeypatch):
    from save_helpers import FakeTransport

    import bgwcli.client as client_module
    from bgwcli.client import BGW320Client

    full = "<title>Login</title><p>All web server sessions are in use</p>"

    def handle(request, n):
        return html(SITEMAP if urlsplit(request.url).path == "/cgi-bin/sitemap.ha" else full)

    transport = FakeTransport(handle)

    def factory(options, access_code, **kwargs):
        client = BGW320Client.from_options(options, access_code, transport=transport, **kwargs)
        client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
        return client

    monkeypatch.setattr(cli, "_client_factory", factory)
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: None)
    argv = ["check", "--host", "http://router.local", "--wait-for-session", "--session-wait-timeout", "5",
            "--session-wait-interval", "1", "--json"]
    code = cli.main(argv)
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["sessionPoolFull"] is True and "authenticated" not in payload
    assert sum(urlsplit(r.url).path == "/cgi-bin/login.ha" for r in transport.requests) >= 1
    assert not any(r.method == "POST" for r in transport.requests)
    cooldown = json.loads(session.session_paths("http://router.local").cooldown.read_text())
    assert cooldown["waitedMs"] == payload["waitedMs"] and cooldown["retryCount"] == payload["retryCount"]


@pytest.mark.parametrize("answer", ["login-page", "login-redirect", "http-403"])
def test_check_forgets_a_dead_session_instead_of_caching_it(tmp_env, capsys, monkeypatch, answer):
    from bgwcli.session import session_paths

    def handle(request, n):
        path = urlsplit(request.url).path
        if path == "/cgi-bin/sitemap.ha":
            return html(SITEMAP)
        if path == "/cgi-bin/login.ha":
            return html(LOGIN, headers={"set-cookie": "SessionID=fresh; Path=/"})
        if answer == "login-redirect":
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(LOGIN, status=403 if answer == "http-403" else 200)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["check", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["authenticated"] is False
    assert client.has_authenticated_session() is False
    assert not session_paths(client.session_identity()).cache.exists()
    assert not any(r.method == "POST" for r in wire.requests), "check never logs in"
