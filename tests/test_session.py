"""Tests for bgwcli.session, ported from BGW320-CLI tests/session.test.ts plus file-format contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

from bgwcli.client import BGW320Client, RawResponse, session_pool_full_error
from bgwcli.errors import RouterSessionPoolFullError
from bgwcli.session import (
    SessionCoordinatorOptions,
    SessionLockTimeoutError,
    SessionState,
    clear_session_state,
    read_session_state,
    router_session_identity,
    session_paths,
    with_router_session,
)
from bgwcli.types import RouterSessionSnapshot

POOL_FULL_HTML = "<title>Login</title><p>all web server sessions are in use</p>"
ORIGIN = "http://router.local"
KEY = hashlib.sha256(ORIGIN.encode()).hexdigest()[:24]


@pytest.fixture(autouse=True)
def stub_parser(monkeypatch):
    module = types.ModuleType("bgwcli.parser")

    def looks_like_login(html: str) -> bool:
        title = re.search(r"<title\b[^>]*>([\s\S]*?)</title>", html, re.I)
        return bool(title and title.group(1).strip().lower() == "login") or "Access Code Required" in html

    module.looks_like_login = looks_like_login
    monkeypatch.setitem(sys.modules, "bgwcli.parser", module)


@pytest.fixture
def cache_dir(tmp_env) -> Path:
    return tmp_env / "cache"


def login_nonce_html(nonce: str) -> str:
    return f'<title>Login</title><form><input name="nonce" value="{nonce}"><input name="password"></form>'


def html(body: str, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    pairs = [("content-type", "text/html")] + list((headers or {}).items())
    return RawResponse(status=status, reason="OK", headers=pairs, body=body.encode())


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    def __call__(self, request):
        from urllib.parse import urlsplit

        self.calls.append(f"{request.method} {urlsplit(request.url).path} {request.headers.get('Cookie', '')}")
        return self.handler(request)


def make_client(transport) -> BGW320Client:
    return BGW320Client(
        ORIGIN, access_code="12345", timeout_ms=1000, insecure_tls=True, user_agent="test", transport=transport
    )


def options(**overrides) -> SessionCoordinatorOptions:
    base = dict(cache_ttl_ms=120000, pool_cooldown_ms=300000, lock_timeout_ms=1000, wait_for_session=False)
    base.update(overrides)
    return SessionCoordinatorOptions(**base)


def now_ms() -> int:
    return int(time.time() * 1000)


# --- identity + paths -----------------------------------------------------------------


def test_router_session_identity_normalises_host():
    assert router_session_identity("router.local") == "https://router.local"
    assert router_session_identity("http://router.local///") == ORIGIN
    assert router_session_identity("https://192.168.1.254:443") == "https://192.168.1.254"


def test_session_paths_use_sha256_prefix_and_env_dir(cache_dir, monkeypatch):
    paths = session_paths(ORIGIN)
    assert paths.origin == ORIGIN
    assert paths.cache == cache_dir / f"{KEY}.session.json"
    assert paths.cooldown == cache_dir / f"{KEY}.cooldown.json"
    assert paths.lock == cache_dir / f"{KEY}.lock"

    monkeypatch.delenv("BGW_SESSION_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", "/xdg")
    assert session_paths(ORIGIN).cache == Path("/xdg/bgw") / f"{KEY}.session.json"

    monkeypatch.delenv("XDG_CACHE_HOME")
    assert session_paths(ORIGIN).cache == Path.home() / ".cache" / "bgw" / f"{KEY}.session.json"


def test_coordinator_options_from_global_options():
    from bgwcli.config import GlobalOptions

    opts = SessionCoordinatorOptions.from_options(
        GlobalOptions(
            session_cache_ttl_ms=1, session_pool_cooldown_ms=2, session_lock_timeout_ms=3, wait_for_session=True
        )
    )
    assert opts == SessionCoordinatorOptions(
        cache_ttl_ms=1, pool_cooldown_ms=2, lock_timeout_ms=3, wait_for_session=True
    )


# --- coordinator ------------------------------------------------------------------------


def test_coordinator_reuses_cached_router_session_across_commands(cache_dir):
    def handler(req):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=abc; Path=/"})
        if req.url.endswith("/cgi-bin/diag.ha"):
            return html("<title>diag</title>")
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    first = make_client(transport)
    with_router_session(first, options(), first.login)
    second = make_client(transport)
    with_router_session(second, options(), lambda: second.get_cgi_page("diag"))

    assert transport.calls == [
        "GET /cgi-bin/login.ha ",
        "POST /cgi-bin/login.ha ",
        "GET /cgi-bin/diag.ha sid=abc",
    ]
    assert oct(cache_dir.stat().st_mode & 0o777) == "0o700"
    cache_file = cache_dir / f"{KEY}.session.json"
    assert oct(cache_file.stat().st_mode & 0o777) == "0o600"
    assert not (cache_dir / f"{KEY}.lock").exists()


def test_session_cache_json_shape_matches_ts_cli(cache_dir):
    client = make_client(FakeTransport(lambda req: html("")))
    client.import_session(RouterSessionSnapshot(origin=ORIGIN, authenticated=True, cookies={"sid": "abc"}))
    before = now_ms()
    with_router_session(client, options(cache_ttl_ms=120000), lambda: "ok")
    raw = (cache_dir / f"{KEY}.session.json").read_text()
    assert raw.endswith("\n") and "\n" not in raw[:-1] and ": " not in raw
    data = json.loads(raw)
    assert list(data) == ["origin", "authenticated", "cookies", "version", "cachedAt", "expiresAt"]
    assert data["origin"] == ORIGIN
    assert data["authenticated"] is True
    assert data["cookies"] == {"sid": "abc"}
    assert data["version"] == 1
    assert isinstance(data["cachedAt"], int) and before <= data["cachedAt"] <= now_ms()
    assert data["expiresAt"] == data["cachedAt"] + 120000


def test_coordinator_imports_ts_written_cache_and_skips_expired(cache_dir):
    cache_dir.mkdir(parents=True)
    fresh = {
        "origin": ORIGIN,
        "authenticated": True,
        "cookies": {"sid": "ts"},
        "version": 1,
        "cachedAt": now_ms(),
        "expiresAt": now_ms() + 60000,
    }
    (cache_dir / f"{KEY}.session.json").write_text(json.dumps(fresh) + "\n")
    client = make_client(FakeTransport(lambda req: html("")))
    with_router_session(client, options(), lambda: None)
    assert client.export_session().cookies == {"sid": "ts"}

    stale = dict(fresh, expiresAt=now_ms() - 1)
    (cache_dir / f"{KEY}.session.json").write_text(json.dumps(stale) + "\n")
    client = make_client(FakeTransport(lambda req: html("")))
    with_router_session(client, options(), lambda: None)
    assert not client.has_authenticated_session()


def test_coordinator_cooldown_prevents_repeated_pool_full_login_attempts(cache_dir):
    transport = FakeTransport(lambda req: html(POOL_FULL_HTML))
    first = make_client(transport)
    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(first, options(), first.login)
    second = make_client(transport)
    with pytest.raises(RouterSessionPoolFullError, match="cooldown is active"):
        with_router_session(second, options(), second.login)
    assert len(transport.calls) == 1

    cooldown = json.loads((cache_dir / f"{KEY}.cooldown.json").read_text())
    assert list(cooldown) == ["version", "origin", "until", "waitedMs", "retryCount"]
    assert cooldown["version"] == 1 and cooldown["origin"] == ORIGIN
    assert cooldown["waitedMs"] == 0 and cooldown["retryCount"] == 0
    assert now_ms() < cooldown["until"] <= now_ms() + 300000
    assert not (cache_dir / f"{KEY}.session.json").exists()


def test_wait_for_session_respects_an_active_local_cooldown(cache_dir):
    transport = FakeTransport(lambda req: html(POOL_FULL_HTML))
    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(make_client(transport), options(), make_client(transport).login)
    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(make_client(transport), options(wait_for_session=True), make_client(transport).login)
    assert len(transport.calls) == 1


def test_expired_cooldown_is_removed_and_run_proceeds(cache_dir):
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{KEY}.cooldown.json").write_text(
        json.dumps({"version": 1, "origin": ORIGIN, "until": now_ms() - 1, "waitedMs": 0, "retryCount": 0})
    )
    assert with_router_session(make_client(FakeTransport(lambda req: html(""))), options(), lambda: 42) == 42
    assert not (cache_dir / f"{KEY}.cooldown.json").exists()


def test_result_carrying_session_pool_full_flag_records_cooldown(cache_dir):
    client = make_client(FakeTransport(lambda req: html("")))
    client.import_session(RouterSessionSnapshot(origin=ORIGIN, authenticated=True, cookies={"sid": "abc"}))
    result = [{"page": "home", "ok": True}, {"page": "diag", "sessionPoolFull": True, "waitedMs": 5}]
    assert with_router_session(client, options(), lambda: result) is result
    assert (cache_dir / f"{KEY}.cooldown.json").exists()
    assert not (cache_dir / f"{KEY}.session.json").exists()
    assert not client.has_authenticated_session()


def test_successful_run_clears_cooldown_and_non_pool_errors_propagate(cache_dir):
    cache_dir.mkdir(parents=True)
    cooldown = cache_dir / f"{KEY}.cooldown.json"
    cooldown.write_text(
        json.dumps({"version": 1, "origin": ORIGIN, "until": now_ms() - 1, "waitedMs": 0, "retryCount": 0})
    )
    client = make_client(FakeTransport(lambda req: html("")))
    client.import_session(RouterSessionSnapshot(origin=ORIGIN, authenticated=True, cookies={"sid": "abc"}))
    with_router_session(client, options(), lambda: "ok")
    assert not cooldown.exists()

    def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        with_router_session(client, options(), fail)
    assert not (cache_dir / f"{KEY}.lock").exists()


def test_session_cache_atomically_replaces_symlink_without_writing_target(cache_dir):
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / f"{KEY}.session.json"
    target = cache_dir / "target.txt"
    target.write_text("unchanged\n")
    os.symlink(target, cache_path)

    client = make_client(FakeTransport(lambda req: html("")))
    client.import_session(RouterSessionSnapshot(origin=ORIGIN, authenticated=True, cookies={"sid": "abc"}))
    with_router_session(client, options(), lambda: "ok")

    assert target.read_text() == "unchanged\n"
    assert cache_path.is_file() and not cache_path.is_symlink()


def test_symlinked_or_corrupt_cache_is_ignored_on_read(cache_dir):
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / f"{KEY}.session.json"
    cache_path.write_text("{not json")
    assert read_session_state(ORIGIN) == SessionState(cached=False)
    cache_path.unlink()
    target = cache_dir / "t.json"
    target.write_text(
        json.dumps({"origin": ORIGIN, "authenticated": True, "cookies": {}, "expiresAt": now_ms() + 9999})
    )
    os.symlink(target, cache_path)
    assert read_session_state(ORIGIN).cached is False


# --- state + clear -------------------------------------------------------------------------


def test_read_session_state_reports_cache_and_cooldown(cache_dir):
    assert read_session_state(ORIGIN) == SessionState(cached=False, cache_expires_at=None, pool_cooldown_until=None)
    cache_dir.mkdir(parents=True)
    expires = now_ms() + 50000
    until = now_ms() + 70000
    (cache_dir / f"{KEY}.session.json").write_text(
        json.dumps(
            {
                "origin": ORIGIN,
                "authenticated": True,
                "cookies": {"a": "b"},
                "version": 1,
                "cachedAt": 0,
                "expiresAt": expires,
            }
        )
    )
    (cache_dir / f"{KEY}.cooldown.json").write_text(
        json.dumps({"version": 1, "origin": ORIGIN, "until": until, "waitedMs": 1, "retryCount": 2})
    )
    assert read_session_state(ORIGIN) == SessionState(cached=True, cache_expires_at=expires, pool_cooldown_until=until)


def test_clear_session_state_removes_local_cache_and_cooldown(cache_dir):
    clear_session_state(ORIGIN)  # nothing there yet: must not fail
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{KEY}.session.json").write_text("{}")
    (cache_dir / f"{KEY}.cooldown.json").write_text("{}")
    clear_session_state(ORIGIN, lock_timeout_ms=1000)
    assert not (cache_dir / f"{KEY}.session.json").exists()
    assert not (cache_dir / f"{KEY}.cooldown.json").exists()
    assert not (cache_dir / f"{KEY}.lock").exists()


# --- lock -------------------------------------------------------------------------------------


def test_lock_contention_times_out_and_stale_lock_is_reclaimed(cache_dir, monkeypatch):
    cache_dir.mkdir(parents=True)
    lock = cache_dir / f"{KEY}.lock"
    lock.write_text(json.dumps({"pid": 1, "createdAt": now_ms()}))

    client = make_client(FakeTransport(lambda req: html("")))
    started = time.monotonic()
    with pytest.raises(SessionLockTimeoutError):
        with_router_session(client, options(lock_timeout_ms=250), lambda: "never")
    assert 0.2 <= time.monotonic() - started < 3
    assert lock.exists()

    monkeypatch.setenv("BGW_SESSION_LOCK_STALE_MS", "1")
    old = time.time() - 5
    os.utime(lock, (old, old))
    assert with_router_session(client, options(lock_timeout_ms=1000), lambda: "ran") == "ran"
    assert not lock.exists()


def test_lock_file_content_and_release(cache_dir):
    seen = {}

    def run():
        lock = cache_dir / f"{KEY}.lock"
        seen["content"] = json.loads(lock.read_text())
        seen["mode"] = oct(lock.stat().st_mode & 0o777)
        return "ok"

    with_router_session(make_client(FakeTransport(lambda req: html(""))), options(), run)
    assert seen["content"]["pid"] == os.getpid()
    assert isinstance(seen["content"]["createdAt"], int)
    assert list(seen["content"]) == ["pid", "createdAt"]
    assert seen["mode"] == "0o600"


def test_pool_full_error_metadata_is_persisted_in_cooldown(cache_dir):
    def run():
        raise session_pool_full_error(waited_ms=120000, retry_count=12)

    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(make_client(FakeTransport(lambda req: html(""))), options(pool_cooldown_ms=1000), run)
    cooldown = json.loads((cache_dir / f"{KEY}.cooldown.json").read_text())
    assert cooldown["waitedMs"] == 120000 and cooldown["retryCount"] == 12
    assert cooldown["until"] <= now_ms() + 1000

    with pytest.raises(RouterSessionPoolFullError) as info:
        with_router_session(make_client(FakeTransport(lambda req: html(""))), options(), lambda: "x")
    assert info.value.waited_ms == 120000 and info.value.retry_count == 12
