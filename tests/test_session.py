"""Tests for bgwcli.session, ported from BGW320-CLI tests/session.test.ts plus file-format contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from bgwcli.client import BGW320Client, RawResponse, session_pool_full_error
from bgwcli.errors import (
    RouterAuthError,
    RouterConnectionError,
    RouterSessionPoolFullError,
    UsageError,
    is_page_level_auth_error,
)
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
    # A live PID (including PID 1) must never be reclaimed solely because of its age.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=5)
    lock.write_text(json.dumps({"pid": child.pid, "createdAt": now_ms()}))
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
    assert isinstance(seen["content"]["token"], str) and seen["content"]["token"]
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


# --- cache lifetime across failing commands -------------------------------------------------


def write_cached_session(cache_dir: Path, sid: str = "dead") -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{KEY}.session.json"
    record = {
        "origin": ORIGIN,
        "authenticated": True,
        "cookies": {"sid": sid},
        "version": 1,
        "cachedAt": now_ms(),
        "expiresAt": now_ms() + 60000,
    }
    path.write_text(json.dumps(record) + "\n")
    return path


def test_cached_session_bounced_to_login_is_dropped_from_the_cache(cache_dir):
    cache_file = write_cached_session(cache_dir)

    def handler(req):
        if req.method == "POST":
            return html("<title>Login</title><p>Login Failed</p>")
        return html(login_nonce_html("abc123"), status=401)

    client = make_client(FakeTransport(handler))
    with pytest.raises(RouterAuthError) as info:
        with_router_session(client, options(), lambda: client.get_cgi_page("diag"))
    assert not is_page_level_auth_error(info.value)
    assert not cache_file.exists()
    assert not (cache_dir / f"{KEY}.cooldown.json").exists()


def test_session_wide_auth_error_escaping_the_command_unlinks_the_cache(cache_dir):
    cache_file = write_cached_session(cache_dir)
    client = make_client(FakeTransport(lambda req: html("")))

    def run():
        raise RouterAuthError("Router returned the login page after authentication.")

    with pytest.raises(RouterAuthError):
        with_router_session(client, options(), run)
    assert not cache_file.exists()
    assert not client.has_authenticated_session()


def test_page_level_auth_error_keeps_the_cached_session(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="live")

    def handler(req):
        return html("<title>Forbidden</title>", status=403)

    client = make_client(FakeTransport(handler))
    with pytest.raises(RouterAuthError) as info:
        with_router_session(client, options(), lambda: client.get_cgi_page("diag"))
    assert is_page_level_auth_error(info.value)
    assert json.loads(cache_file.read_text())["cookies"] == {"sid": "live"}


def login_handler(req):
    if req.method == "POST":
        return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=fresh; Path=/"})
    return html(login_nonce_html("abc123"))


@pytest.mark.parametrize("failure", [UsageError("rejected"), ValueError("snapshot")])
def test_fresh_login_is_persisted_when_the_command_then_fails(cache_dir, failure):
    transport = FakeTransport(login_handler)
    client = make_client(transport)

    def run():
        client.login()
        raise failure

    with pytest.raises(type(failure)):
        with_router_session(client, options(), run)
    data = json.loads((cache_dir / f"{KEY}.session.json").read_text())
    assert data["cookies"] == {"sid": "fresh"} and data["authenticated"] is True
    assert not (cache_dir / f"{KEY}.lock").exists()

    # The next command reuses the persisted session instead of taking another router slot.
    second = make_client(transport)
    with_router_session(second, options(), lambda: None)
    assert second.export_session().cookies == {"sid": "fresh"}
    assert [call for call in transport.calls if call.startswith("POST")] == ["POST /cgi-bin/login.ha "]


def test_failed_command_without_a_session_writes_no_cache(cache_dir):
    client = make_client(FakeTransport(lambda req: html("")))

    def run():
        raise UsageError("bad")

    with pytest.raises(UsageError):
        with_router_session(client, options(), run)
    assert not (cache_dir / f"{KEY}.session.json").exists()


def test_persist_failure_after_a_failed_command_warns_and_keeps_the_error(cache_dir, monkeypatch, capsys):
    import bgwcli.session as session_module

    client = make_client(FakeTransport(login_handler))

    def run():
        client.login()
        raise UsageError("rejected")

    def broken_write(path, value):
        raise OSError("disk full")

    monkeypatch.setattr(session_module, "_write_json", broken_write)
    with pytest.raises(UsageError, match="rejected"):
        with_router_session(client, options(), run)
    assert "Could not persist local router session" in capsys.readouterr().err


def test_failed_run_on_an_imported_session_does_not_restamp_the_cache(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="cached")
    os.utime(cache_file, (time.time() - 30,) * 2)
    before_text, before_mtime = cache_file.read_text(), cache_file.stat().st_mtime_ns
    client = make_client(FakeTransport(lambda req: pytest.fail("no router request expected")))

    def run():
        raise RouterConnectionError("router unreachable")

    with pytest.raises(RouterConnectionError):
        with_router_session(client, options(), run)
    # The imported session was never verified this run: its recorded lifetime must not grow.
    assert cache_file.read_text() == before_text
    assert cache_file.stat().st_mtime_ns == before_mtime


def test_failed_run_after_a_bounce_and_fresh_login_rewrites_the_cache(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="dead")
    state = {"logged_in": False}

    def handler(req):
        if req.method == "POST":
            state["logged_in"] = True
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=fresh; Path=/"})
        if req.url.endswith("/cgi-bin/diag.ha") and state["logged_in"]:
            return html("<title>diag</title>")
        return html(login_nonce_html("abc123"))

    client = make_client(FakeTransport(handler))

    def run():
        client.get_cgi_page("diag")
        raise UsageError("rejected")

    with pytest.raises(UsageError):
        with_router_session(client, options(), run)
    assert json.loads(cache_file.read_text())["cookies"] == {"sid": "fresh"}


def test_successful_run_without_router_contact_does_not_restamp_the_imported_session(cache_dir):
    """A run that never used the imported session proved nothing about it: its recorded lifetime
    must not grow (the gateway may already have expired it)."""
    cache_file = write_cached_session(cache_dir, sid="cached")
    os.utime(cache_file, (time.time() - 30,) * 2)
    before_text, before_mtime = cache_file.read_text(), cache_file.stat().st_mtime_ns
    client = make_client(FakeTransport(lambda req: pytest.fail("no router request expected")))
    assert with_router_session(client, options(), lambda: "local only") == "local only"
    assert cache_file.read_text() == before_text
    assert cache_file.stat().st_mtime_ns == before_mtime


def test_successful_unauthenticated_request_does_not_restamp_the_imported_session(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="cached")
    before_text = cache_file.read_text()
    client = make_client(FakeTransport(lambda req: html("<title>Site Map</title>")))
    with_router_session(client, options(), lambda: client.get_cgi_page("sitemap", auth=False))
    assert cache_file.read_text() == before_text


def test_successful_authenticated_request_restamps_the_imported_session(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="cached")
    before = json.loads(cache_file.read_text())
    time.sleep(0.002)
    client = make_client(FakeTransport(lambda req: html("<title>diag</title>")))
    with_router_session(client, options(cache_ttl_ms=120000), lambda: client.get_cgi_page("diag"))
    after = json.loads(cache_file.read_text())
    assert after["cookies"] == {"sid": "cached"} and after["cachedAt"] > before["cachedAt"]
    assert after["expiresAt"] == after["cachedAt"] + 120000


def test_expired_cooldown_that_cannot_be_removed_warns_and_the_run_proceeds(cache_dir, monkeypatch, capsys):
    """An expired cooldown no longer blocks anything: failing to delete it is cache housekeeping,
    reported as a warning, never the command's outcome."""
    cache_dir.mkdir(parents=True)
    cooldown = cache_dir / f"{KEY}.cooldown.json"
    record = {"version": 1, "origin": ORIGIN, "until": now_ms() - 1, "waitedMs": 0, "retryCount": 0}
    cooldown.write_text(json.dumps(record))
    original_unlink = Path.unlink

    def denied_unlink(self, *args, **kwargs):
        if self == cooldown:
            raise PermissionError(1, "Operation not permitted", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    assert with_router_session(make_client(FakeTransport(lambda req: html(""))), options(), lambda: 42) == 42
    err = capsys.readouterr().err
    assert "warning: Could not remove expired local session cooldown" in err and str(cooldown) in err


def test_live_cooldown_still_refuses_the_run(cache_dir):
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{KEY}.cooldown.json").write_text(
        json.dumps({"version": 1, "origin": ORIGIN, "until": now_ms() + 60000, "waitedMs": 3, "retryCount": 1})
    )
    with pytest.raises(RouterSessionPoolFullError, match="local cooldown is active"):
        with_router_session(make_client(FakeTransport(lambda req: html(""))), options(), lambda: pytest.fail("refused"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_unreadable_owned_cache_file_is_nothing_cached_and_is_replaced(cache_dir, capsys):
    """Our own cache file we cannot read holds nothing usable: warn, log in afresh, and let the
    atomic replace publish the new session over it (the cooldown file keeps its strictness)."""
    cache_file = write_cached_session(cache_dir, sid="unreadable")
    cache_file.chmod(0)

    def handler(req):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=fresh; Path=/"})
        if req.url.endswith("/cgi-bin/diag.ha"):
            return html("<title>diag</title>")
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    client = make_client(transport)
    with_router_session(client, options(), lambda: (client.login(), client.get_cgi_page("diag")))
    err = capsys.readouterr().err
    assert "warning: Ignoring unreadable cached router session" in err and str(cache_file) in err
    assert all("sid=unreadable" not in call for call in transport.calls)
    assert json.loads(cache_file.read_text())["cookies"] == {"sid": "fresh"}
    assert oct(cache_file.stat().st_mode & 0o777) == "0o600"


def test_stale_marker_is_judged_from_the_inode_held_open_not_by_pathname(cache_dir, monkeypatch):
    """The flock and the inode check are on the descriptor opened for inspection; the ownership
    record must come from that same descriptor, never a second pathname lookup that could land on
    a replacement marker."""
    from bgwcli import session as session_module

    cache_dir.mkdir(parents=True)
    lock = cache_dir / f"{KEY}.lock"
    lock.write_text(json.dumps({"pid": 999999, "createdAt": now_ms(), "token": "old", "ownership": "flock-v1"}))
    original_read_json = session_module._read_json
    inspecting = {"active": False}
    original_remove = session_module._remove_stale_lock

    def remove_stale_lock(path, stale_ms):
        inspecting["active"] = True
        try:
            return original_remove(path, stale_ms)
        finally:
            inspecting["active"] = False

    def read_json(path, *args, **kwargs):
        if inspecting["active"] and path == lock:
            raise AssertionError("stale-lock inspection re-read the marker by pathname")
        return original_read_json(path, *args, **kwargs)

    monkeypatch.setattr(session_module, "_remove_stale_lock", remove_stale_lock)
    monkeypatch.setattr(session_module, "_read_json", read_json)
    client = make_client(FakeTransport(lambda req: html("")))
    assert with_router_session(client, options(lock_timeout_ms=1000), lambda: "ran") == "ran"
    assert not lock.exists()


# --- session probe (check) --------------------------------------------------------------------


def _probe_handler(answer):
    def handler(req):
        assert req.method == "GET", "the probe never logs in or writes"
        if req.url.endswith("/cgi-bin/services.ha"):
            return answer(req)
        if req.url.endswith("/cgi-bin/login.ha"):
            return html(login_nonce_html("abc123"), headers={"set-cookie": "SessionID=fresh; Path=/"})
        raise AssertionError(req.url)

    return handler


def test_probe_of_a_dead_cached_session_forgets_it_and_keeps_the_login_cookie_out(cache_dir):
    from bgwcli.client import SESSION_PROBE_PAGE

    assert SESSION_PROBE_PAGE == "services"
    cache_file = write_cached_session(cache_dir)
    bounce = html("", status=302, headers={"location": "/cgi-bin/login.ha"})
    transport = FakeTransport(_probe_handler(lambda req: bounce))
    client = make_client(transport)
    accepted = with_router_session(client, options(), client.probe_session_accepted)
    assert accepted is False
    assert client.has_authenticated_session() is False
    assert client.export_session().cookies == {}
    assert not cache_file.exists()
    assert transport.calls == ["GET /cgi-bin/services.ha sid=dead", "GET /cgi-bin/login.ha sid=dead"]


def test_probe_of_a_live_cached_session_keeps_it_without_restamping(cache_dir):
    cache_file = write_cached_session(cache_dir, sid="live")
    before = cache_file.read_text()
    client = make_client(FakeTransport(_probe_handler(lambda req: html("<title>Services</title>"))))
    assert with_router_session(client, options(), client.probe_session_accepted) is True
    assert client.has_authenticated_session() is True
    assert cache_file.read_text() == before


def test_probe_answered_by_an_http_error_is_not_accepted_and_clears_the_session(cache_dir):
    cache_file = write_cached_session(cache_dir)
    client = make_client(FakeTransport(_probe_handler(lambda req: html("<title>Forbidden</title>", status=403))))
    assert with_router_session(client, options(), client.probe_session_accepted) is False
    assert client.has_authenticated_session() is False
    assert not cache_file.exists()


def _reset(req):
    raise ConnectionResetError("reset by peer")


@pytest.mark.parametrize("answer", [_reset, lambda req: html("oops", status=500)], ids=["transport", "http-500"])
def test_probe_without_an_answer_is_not_accepted_but_keeps_the_cached_session(cache_dir, answer):
    """A transport fault or a server error says nothing about the session: report not
    authenticated, but neither forget the cached session nor re-stamp it."""
    cache_file = write_cached_session(cache_dir, sid="maybe")
    before = cache_file.read_text()
    client = make_client(FakeTransport(_probe_handler(answer)))
    assert with_router_session(client, options(), client.probe_session_accepted) is False
    assert client.export_session().cookies == {"sid": "maybe"}
    assert cache_file.read_text() == before


def test_probe_answered_by_a_full_pool_is_no_answer(cache_dir):
    write_cached_session(cache_dir)
    client = make_client(FakeTransport(_probe_handler(lambda req: html(POOL_FULL_HTML))))
    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(client, options(), client.probe_session_accepted)
    assert (cache_dir / f"{KEY}.cooldown.json").exists()


def test_probe_waits_for_a_free_slot_and_probes_once_more_with_wait_for_session(cache_dir, monkeypatch):
    import bgwcli.client as client_module

    monkeypatch.setattr(client_module, "_sleep", lambda seconds: None)
    write_cached_session(cache_dir, sid="live")
    answers = iter([html(POOL_FULL_HTML), html("<title>Services</title>")])
    transport = FakeTransport(_probe_handler(lambda req: next(answers)))
    client = BGW320Client(
        ORIGIN, access_code="12345", timeout_ms=1000, user_agent="test", transport=transport,
        wait_for_session=True, session_wait_timeout_ms=5000, session_wait_interval_ms=1,
    )
    assert with_router_session(client, options(wait_for_session=True), client.probe_session_accepted) is True
    assert [call.split()[1] for call in transport.calls] == [
        "/cgi-bin/services.ha", "/cgi-bin/login.ha", "/cgi-bin/services.ha",
    ]


# --- wall-clock sanity cap ----------------------------------------------------------------------


def _write_record(cache_dir: Path, suffix: str, record: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{KEY}.{suffix}.json"
    path.write_text(json.dumps(record) + "\n")
    return path


def test_cooldown_further_ahead_than_the_cooldown_length_is_treated_as_expired(cache_dir):
    """A record stamped while the wall clock was far ahead (or a hand-edited one) would otherwise block
    every run until the clock catches up: anything beyond one full cooldown from now is expired."""
    path = _write_record(
        cache_dir, "cooldown", {"version": 1, "origin": ORIGIN, "until": now_ms() + 10 * 3600 * 1000}
    )
    client = make_client(FakeTransport(lambda req: html("<title>diag</title>")))
    with_router_session(client, options(pool_cooldown_ms=300000), lambda: client.get_cgi_page("diag", auth=False))
    assert not path.exists()
    assert read_session_state(ORIGIN, pool_cooldown_ms=300000).pool_cooldown_until is None


def test_cooldown_within_its_length_still_refuses_the_run(cache_dir):
    _write_record(cache_dir, "cooldown", {"version": 1, "origin": ORIGIN, "until": now_ms() + 200000})
    client = make_client(FakeTransport(lambda req: html("")))
    with pytest.raises(RouterSessionPoolFullError):
        with_router_session(client, options(pool_cooldown_ms=300000), lambda: None)
    assert read_session_state(ORIGIN, pool_cooldown_ms=300000).pool_cooldown_until is not None


@pytest.mark.parametrize("stamp", [{"cachedAt": "future"}, {}], ids=["stamped-in-the-future", "unstamped"])
def test_cached_session_from_a_clock_that_was_ahead_is_not_imported(cache_dir, stamp):
    """A cache whose cachedAt is ahead of now was stamped while the clock was ahead; an unstamped
    record further ahead than one TTL is capped the same way."""
    record = {
        "origin": ORIGIN, "authenticated": True, "cookies": {"sid": "future"}, "version": 1,
        "expiresAt": now_ms() + 10 * 3600 * 1000,
    }
    if stamp:
        record["cachedAt"] = now_ms() + 10 * 3600 * 1000 - 120000
    _write_record(cache_dir, "session", record)
    assert read_session_state(ORIGIN, cache_ttl_ms=120000).cached is False
    if not stamp:
        # Without a known TTL the status view keeps an unstamped record's lifetime.
        assert read_session_state(ORIGIN).cached is True
    transport = FakeTransport(lambda req: html("<title>diag</title>"))
    client = make_client(transport)
    with_router_session(client, options(cache_ttl_ms=120000), lambda: client.get_cgi_page("diag", auth=False))
    assert transport.calls == ["GET /cgi-bin/diag.ha "]


def test_stamped_cache_written_under_a_longer_ttl_keeps_its_own_lifetime(cache_dir):
    _write_record(cache_dir, "session", {
        "origin": ORIGIN, "authenticated": True, "cookies": {"sid": "long"}, "version": 1,
        "cachedAt": now_ms() - 1000, "expiresAt": now_ms() + 600_000,
    })
    assert read_session_state(ORIGIN, cache_ttl_ms=120000).cached is True
