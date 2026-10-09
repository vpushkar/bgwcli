"""Every session-wide failure drops the cookies, the authenticated flag and the cached copy together."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli, session
from bgwcli.client import RouterResponseError
from bgwcli.errors import RouterAuthError, RouterSessionPoolFullError

LOGIN = '<title>Login</title><input name="nonce" value="abc123">'
POOL_FULL = "<title>Login</title><p>All web server sessions are in use</p>"


def _cache(client):
    paths = session.session_paths(client.session_identity())
    session._ensure_private_dir(paths.cache.parent)
    now = session._now_ms()
    session._write_json(paths.cache, {"origin": client.session_identity(), "authenticated": True,
                                      "cookies": {"sid": "old"}, "version": 1, "cachedAt": now,
                                      "expiresAt": now + 120000})
    return paths


@pytest.mark.parametrize("status", [300, 404, 500, 503])
def test_a_retry_that_ends_at_login_ha_forgets_the_session_whatever_the_status(tmp_env, monkeypatch, capsys, status):
    services_gets = 0

    def wire(request, number):
        nonlocal services_gets
        path = urlsplit(request.url).path
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/home.ha", "set-cookie": "sid=new"})
        if path == "/cgi-bin/services.ha":
            services_gets += 1
            if services_gets == 1:
                return html(LOGIN)
            return html("", 302, {"location": "/cgi-bin/login.ha"})
        return html("session refused", status)

    client, _ = client_with(wire)
    paths = _cache(client)
    monkeypatch.setattr(cli, "_client_factory", lambda *_a, **_k: client)
    assert cli.main(["page", "services", "--json"]) == 2
    capsys.readouterr()
    assert not client.has_authenticated_session() and client._cookies == {}
    assert not paths.cache.exists()


@pytest.mark.parametrize("status", [500, 503])
def test_a_first_answer_from_login_ha_with_an_http_error_forgets_the_session(tmp_env, monkeypatch, capsys, status):
    def wire(request, number):
        return html("", 302, {"location": "/cgi-bin/login.ha"}) if number == 1 else html("down", status)

    client, _ = client_with(wire)
    paths = _cache(client)
    monkeypatch.setattr(cli, "_client_factory", lambda *_a, **_k: client)
    assert cli.main(["page", "services", "--json"]) == 2
    capsys.readouterr()
    assert not client.has_authenticated_session() and client._cookies == {}
    assert not paths.cache.exists()


def test_a_rejected_operation_clears_the_cookies_with_the_flag():
    def wire(request, number):
        path = urlsplit(request.url).path
        if request.method == "POST" and path != "/cgi-bin/login.ha":
            return html("", 302, {"location": "/cgi-bin/login.ha"})
        return html(
            '<form action="/cgi-bin/etherlan.ha"><input name="nonce" value="abc123">'
            '<input type="submit" name="Save" value="Save"></form>'
        )

    client, _ = client_with(wire)
    with pytest.raises(RouterAuthError):
        client.post_form("etherlan", "etherlan.ha", {"x": "1"})
    assert not client.has_authenticated_session() and client._cookies == {}


def test_a_pool_full_retry_clears_the_cookies_with_the_flag():
    gets = 0

    def wire(request, number):
        nonlocal gets
        path = urlsplit(request.url).path
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/home.ha", "set-cookie": "sid=new"})
        if path == "/cgi-bin/services.ha":
            gets += 1
            return html(LOGIN if gets == 1 else POOL_FULL)
        return html(LOGIN)

    client, _ = client_with(wire)
    with pytest.raises(RouterSessionPoolFullError):
        client.get_cgi_page("services")
    assert not client.has_authenticated_session() and client._cookies == {}


@pytest.mark.parametrize("page", ["sitemap", "services"])
def test_a_public_read_answered_by_the_pool_full_page_raises_pool_full(page):
    client, wire = client_with(lambda *_: html(POOL_FULL))
    client.clear_session()
    with pytest.raises(RouterSessionPoolFullError):
        client.get_cgi_page(page, auth=False)
    with pytest.raises(RouterSessionPoolFullError):
        client.check()
    assert len(wire.requests) == 2


def test_the_probe_still_waits_for_a_free_slot_instead_of_raising_at_once(monkeypatch):
    answers = iter([POOL_FULL, '<title>Services</title><table></table>'])
    client, _ = client_with(lambda *_: html(next(answers)))
    waited = []
    monkeypatch.setattr(client, "wait_for_free_session", lambda: waited.append(True))
    assert client.probe_session_accepted() is True and waited == [True]


def test_a_failing_run_that_loses_an_imported_session_forgets_the_cached_copy(tmp_env):
    client, _ = client_with(lambda *_: html(""))
    paths = _cache(client)
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)

    def run():
        client.clear_session()
        raise RouterResponseError("login.ha is down", status_code=503)

    with pytest.raises(RouterResponseError):
        session.with_router_session(client, options, run)
    assert not paths.cache.exists()


def test_record_pool_full_cooldown_blocks_the_next_coordinated_run_and_drops_the_cache(tmp_env):
    from bgwcli.client import session_pool_full_error

    client, _ = client_with(lambda *_: html(""))
    paths = _cache(client)
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    session.record_pool_full_cooldown(client, session_pool_full_error(waited_ms=5, retry_count=2), options)
    assert not paths.cache.exists() and paths.cooldown.exists() and not client.has_authenticated_session()
    with pytest.raises(RouterSessionPoolFullError, match="cooldown is active"):
        session.with_router_session(client, options, lambda: None)


def test_record_pool_full_cooldown_never_replaces_the_pool_full_error_with_a_configuration_error(
    tmp_env, monkeypatch, capsys
):
    from bgwcli.client import session_pool_full_error

    monkeypatch.setenv("BGW_SESSION_LOCK_STALE_MS", "bogus")
    client, _ = client_with(lambda *_: html(""))
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    session.record_pool_full_cooldown(client, session_pool_full_error(), options)
    assert "pool-full outcome preserved" in capsys.readouterr().err
    assert not client.has_authenticated_session()


def _hold_router_lock(client):
    paths = session.session_paths(client.session_identity())
    session._ensure_private_dir(paths.cache.parent)
    return session._acquire_lock(paths.lock, 1000)


def test_pool_full_on_a_local_command_survives_a_held_lock(tmp_env, monkeypatch, capsys):
    """Recording the cooldown is best-effort: a lock timeout while recording becomes a warning and the
    command still reports the pool-full outcome (exit 2, sessionPoolFull, wait evidence)."""
    import json

    monkeypatch.setenv("BGW_SESSION_LOCK_TIMEOUT_MS", "200")
    client, _ = client_with(lambda *_: html(POOL_FULL))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    release = _hold_router_lock(client)
    try:
        code = cli.main(["--host", "router.local", "--json", "sitemap"])
    finally:
        release()
    captured = capsys.readouterr()
    assert code == 2
    out = json.loads(captured.out)
    assert out["sessionPoolFull"] is True
    assert "waitedMs" in out and "retryCount" in out
    assert "Could not update local session cooldown" in captured.err
    assert not session.session_paths(client.session_identity()).cooldown.exists()


def test_pool_full_on_a_local_command_records_the_cooldown_when_the_lock_is_free(tmp_env, monkeypatch, capsys):
    client, _ = client_with(lambda *_: html(POOL_FULL))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    assert cli.main(["--host", "router.local", "--json", "sitemap"]) == 2
    assert session.session_paths(client.session_identity()).cooldown.exists()
