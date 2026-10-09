"""Offline regressions for authentication, CGI routing and session ownership."""

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from bgwcli import session
from bgwcli.client import BGW320Client, RawResponse, RouterResponseError
from bgwcli.errors import RouterAuthError, UsageError

ORIGIN = "http://router.invalid"
LOGIN = b'<title>Login</title><input name="nonce" value="abc123">'


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize(
    "page",
    [
        "reset.ha?x=1",
        "reset?x=1",
        "reset#x",
        "/cgi-bin/reset.ha",
        "../reset",
        "re%73et",
        "reset%2eha",
        "reset\\ha",
        "https://other/reset",
    ],
)
def test_invalid_cgi_identifiers_are_rejected_before_transport(method, page):
    calls = []

    def transport(request):
        calls.append(request)
        return RawResponse(200, "OK", [], b"<title>Reset</title>")

    client = BGW320Client(ORIGIN, transport=transport)
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "synthetic"}})
    with pytest.raises(UsageError):
        if method == "get":
            client.get_cgi_page(page)
        else:
            client.post_cgi_page(page, {})
    assert calls == []


@pytest.mark.parametrize("stage", ["login-get", "login-post", "protected-probe"])
@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_http_error_cannot_authenticate_or_cache(stage, status, tmp_env):
    calls = []

    def transport(request):
        calls.append(request)
        current = (
            "login-post"
            if request.method == "POST"
            else ("login-get" if request.url.endswith("login.ha") else "protected-probe")
        )
        code = status if current == stage else 200
        body = LOGIN if current == "login-get" else b"<title>Welcome</title>"
        return RawResponse(code, "Synthetic", [("set-cookie", "sid=synthetic")], body)

    client = BGW320Client(ORIGIN, access_code="synthetic", transport=transport)
    error_type = RouterAuthError if status in (401, 403) else RouterResponseError
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(error_type) as caught:
        session.with_router_session(client, opts, client.login)
    assert caught.value.status_code == status
    assert client.has_authenticated_session() is False
    assert client.export_session().authenticated is False
    assert not session.session_paths(ORIGIN).cache.exists()
    assert len(calls) == {"login-get": 1, "login-post": 2, "protected-probe": 3}[stage]


def test_cooldown_refusal_preserves_deadline_without_router_access(tmp_env, monkeypatch):
    paths = session.session_paths(ORIGIN)
    paths.cooldown.parent.mkdir()
    record = {"until": 110000, "waitedMs": 7, "retryCount": 2}
    paths.cooldown.write_text(json.dumps(record))
    monkeypatch.setattr(session, "_now_ms", lambda: 100000)
    calls = []
    client = BGW320Client(ORIGIN, transport=lambda request: calls.append(request))
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(session.RouterSessionPoolFullError):
        session.with_router_session(client, opts, client.login)
    assert json.loads(paths.cooldown.read_text()) == record
    assert calls == []
    assert not paths.lock.exists()


def test_live_lock_owner_cannot_be_displaced_by_age(tmp_path, monkeypatch):
    path = tmp_path / "router.lock"
    release = session._acquire_lock(path, 1000)
    old = time.time() - 901
    os.utime(path, (old, old))
    monkeypatch.setenv("BGW_SESSION_LOCK_STALE_MS", "1")
    try:
        with pytest.raises(session.SessionLockTimeoutError):
            session._acquire_lock(path, 0)
        assert json.loads(path.read_text())["pid"] == os.getpid()
    finally:
        release()


def test_former_owner_release_preserves_replacement_lock(tmp_path):
    path = tmp_path / "router.lock"
    release = session._acquire_lock(path, 1000)
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps({"pid": os.getpid(), "createdAt": 123}))
    replacement.replace(path)
    release()
    assert json.loads(path.read_text()) == {"pid": os.getpid(), "createdAt": 123}


def _lock_worker(path, start, active, overlap, entered):
    start.wait(5)
    release = session._acquire_lock(Path(path), 5000)
    try:
        with active.get_lock():
            active.value += 1
            if active.value > 1:
                overlap.value = 1
        with entered.get_lock():
            entered.value += 1
        # Make an active owner older than the reclamation threshold.
        old = time.time() - 901
        os.utime(path, (old, old))
        time.sleep(0.06)
        with active.get_lock():
            active.value -= 1
    finally:
        release()


def test_stale_reclamation_serializes_concurrent_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "router.lock"
    dead = context.Process(target=time.sleep, args=(0,))
    dead.start()
    dead.join(5)
    assert dead.exitcode == 0
    path.write_text(json.dumps({"pid": dead.pid, "createdAt": 0}))
    old = time.time() - 901
    os.utime(path, (old, old))
    start = context.Event()
    active, overlap, entered = (context.Value("i", 0) for _ in range(3))
    workers = [
        context.Process(target=_lock_worker, args=(str(path), start, active, overlap, entered)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        for worker in workers:
            worker.join(10)
        assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
        assert entered.value == 4
        assert overlap.value == 0
        assert not path.exists()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)


def test_reissued_token_on_same_inode_is_not_released_by_former_owner(tmp_path):
    path = tmp_path / "router.lock"
    release = session._acquire_lock(path, 1000)
    inode = path.stat().st_ino
    owner = json.loads(path.read_text())
    owner["token"] = "replacement-acquisition"
    path.write_text(json.dumps(owner))
    assert path.stat().st_ino == inode
    release()
    assert json.loads(path.read_text())["token"] == "replacement-acquisition"


def test_repeated_release_does_not_delete_next_owner(tmp_path):
    path = tmp_path / "router.lock"
    release_a = session._acquire_lock(path, 1000)
    release_a()
    release_b = session._acquire_lock(path, 1000)
    owner_b = json.loads(path.read_text())
    try:
        release_a()
        assert json.loads(path.read_text()) == owner_b
    finally:
        release_b()


def test_guard_contention_obeys_lock_timeout(tmp_path):
    import fcntl

    path = tmp_path / "router.lock"
    guard = path.with_name("router.lock.guard")
    with guard.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(session.SessionLockTimeoutError):
            session._acquire_lock(path, 100)
        assert 0.08 <= time.monotonic() - started < 1
    assert not path.exists()
    assert guard.stat().st_mode & 0o777 == 0o600


def test_clear_session_state_keeps_permanent_guard_inode(tmp_env):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    release = session._acquire_lock(paths.lock, 1000)
    release()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    inode = guard.stat().st_ino
    session.clear_session_state(ORIGIN)
    assert guard.stat().st_ino == inode


@pytest.mark.parametrize("page", ["DiAg.hA", "diag", "new_page"])
def test_valid_raw_cgi_identifiers_are_canonicalized(page):
    urls = []

    def transport(request):
        urls.append(request.url)
        return RawResponse(200, "OK", [], b"<title>Page</title>")

    client = BGW320Client(ORIGIN, transport=transport)
    client.get_cgi_page(page, auth=False)
    assert urls == [f"http://router.invalid/cgi-bin/{'new_page' if page == 'new_page' else 'diag'}.ha"]


@pytest.mark.parametrize("status", [200, 503])
def test_pool_full_protected_probe_retains_cooldown_classification(status, tmp_env):
    def transport(request):
        if request.url.endswith("services.ha"):
            return RawResponse(status, "Synthetic", [], b"<title>Login</title>all web server sessions are in use")
        body = LOGIN if request.method == "GET" else b"<title>Welcome</title>"
        return RawResponse(200, "OK", [("set-cookie", "sid=synthetic")], body)

    client = BGW320Client(ORIGIN, access_code="synthetic", transport=transport)
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(session.RouterSessionPoolFullError):
        session.with_router_session(client, opts, client.login)
    assert session.session_paths(ORIGIN).cooldown.exists()
    assert client.has_authenticated_session() is False


@pytest.mark.parametrize("status", [200, 503])
def test_protected_get_waits_for_pool_slot_then_authenticates(status, tmp_env, monkeypatch):
    import bgwcli.client as client_module

    now = [1000]
    monkeypatch.setattr(client_module, "_now_ms", lambda: now[0])
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: now.__setitem__(0, now[0] + round(seconds * 1000)))
    waits, requests = [], []
    responses = iter(
        [
            RawResponse(status, "Synthetic", [], b"<title>Login</title>all web server sessions are in use"),
            RawResponse(200, "OK", [("set-cookie", "sid=synthetic")], LOGIN),
            RawResponse(302, "Found", [("location", "/cgi-bin/home.ha")], b""),
            RawResponse(200, "OK", [], b"<title>Custom Services</title>"),
        ]
    )

    def transport(request):
        requests.append((request.method, request.url))
        return next(responses)

    client = BGW320Client(
        ORIGIN,
        access_code="synthetic",
        transport=transport,
        wait_for_session=True,
        session_wait_timeout_ms=100,
        session_wait_interval_ms=1,
        on_session_wait=waits.append,
    )
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, True)
    response = session.with_router_session(client, opts, lambda: client.get_cgi_page("services"))
    assert response.status_code == 200 and response.body == "<title>Custom Services</title>"
    assert client.has_authenticated_session() is True
    assert requests == [
        ("GET", "http://router.invalid/cgi-bin/services.ha"),
        ("GET", "http://router.invalid/cgi-bin/login.ha"),
        ("POST", "http://router.invalid/cgi-bin/login.ha"),
        ("GET", "http://router.invalid/cgi-bin/services.ha"),
    ]
    assert waits == [{"waitedMs": 0, "retryCount": 0, "timeoutMs": 100, "intervalMs": 1}]
    paths = session.session_paths(ORIGIN)
    assert json.loads(paths.cache.read_text())["authenticated"] is True
    assert not paths.cooldown.exists() and not paths.lock.exists()


def test_protected_get_pool_wait_exhaustion_retains_metadata_and_cooldown(tmp_env, monkeypatch):
    import bgwcli.client as client_module

    now = [1000]
    monkeypatch.setattr(client_module, "_now_ms", lambda: now[0])
    monkeypatch.setattr(session, "_now_ms", lambda: now[0])
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: now.__setitem__(0, now[0] + round(seconds * 1000)))
    waits, requests = [], []

    def transport(request):
        requests.append((request.method, request.url))
        return RawResponse(200, "OK", [], b"<title>Login</title>all web server sessions are in use")

    client = BGW320Client(
        ORIGIN,
        access_code="synthetic",
        transport=transport,
        wait_for_session=True,
        session_wait_timeout_ms=100,
        session_wait_interval_ms=40,
        on_session_wait=waits.append,
    )
    opts = session.SessionCoordinatorOptions(120000, 300000, 1000, True)
    with pytest.raises(session.RouterSessionPoolFullError) as caught:
        session.with_router_session(client, opts, lambda: client.get_cgi_page("services"))
    assert caught.value.waited_ms == 100 and caught.value.retry_count == 3
    assert [event["waitedMs"] for event in waits] == [0, 40, 80]
    assert (
        requests
        == [("GET", "http://router.invalid/cgi-bin/services.ha")]
        + [("GET", "http://router.invalid/cgi-bin/login.ha")] * 3
    )
    assert now[0] == 1100
    paths = session.session_paths(ORIGIN)
    cooldown = json.loads(paths.cooldown.read_text())
    assert cooldown["until"] == 301100
    assert cooldown["waitedMs"] == 100 and cooldown["retryCount"] == 3
    assert not paths.cache.exists() and not paths.lock.exists()
    assert client.has_authenticated_session() is False


@pytest.mark.parametrize("status", [200, 503])
def test_protected_get_pool_full_remains_fail_fast_by_default(status):
    requests, waits = [], []

    def transport(request):
        requests.append(request)
        return RawResponse(status, "Synthetic", [], b"<title>Login</title>all web server sessions are in use")

    client = BGW320Client(ORIGIN, access_code="synthetic", transport=transport, on_session_wait=waits.append)
    with pytest.raises(session.RouterSessionPoolFullError) as caught:
        client.get_cgi_page("services")
    assert caught.value.waited_ms == 0 and caught.value.retry_count == 0
    assert len(requests) == 1 and waits == []
    assert client.has_authenticated_session() is False


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_protected_get_wait_option_does_not_hide_ordinary_http_errors(status):
    requests, waits = [], []

    def transport(request):
        requests.append(request)
        return RawResponse(status, "Synthetic", [], LOGIN)

    client = BGW320Client(
        ORIGIN, access_code="synthetic", transport=transport, wait_for_session=True, on_session_wait=waits.append
    )
    error_type = RouterAuthError if status in (401, 403) else RouterResponseError
    with pytest.raises(error_type) as caught:
        client.get_cgi_page("services")
    assert caught.value.status_code == status
    assert len(requests) == 1 and waits == []
    assert client.has_authenticated_session() is False


# --- a login.ha final URL after the re-login is a lost session, whatever the body says ------------

def _login_url_sequence(final_status):
    """Login bounce, a successful re-login, then a retry redirected to login.ha with a plain body."""
    from save_helpers import html

    return [
        html(LOGIN.decode()),
        html("", 302, {"location": "/cgi-bin/home.ha", "set-cookie": "sid=new"}),
        html("", 302, {"location": "/cgi-bin/login.ha"}),
        html("session refused", final_status),
    ]


@pytest.mark.parametrize("final_status", [200, 403])
def test_retry_ending_at_login_url_is_a_session_wide_failure(tmp_env, final_status):
    from save_helpers import client_with

    sequence = _login_url_sequence(final_status)
    client, transport = client_with(lambda _request, n: sequence[n - 1])
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(RouterAuthError) as caught:
        session.with_router_session(client, options, lambda: client.get_cgi_page("services"))
    assert getattr(caught.value, "page_level", False) is not True
    assert client.has_authenticated_session() is False
    assert not session.session_paths(client.session_identity()).cache.exists()
    assert len(transport.calls) == 4


def test_login_probe_ending_at_login_url_fails_the_login(tmp_env):
    from save_helpers import FakeTransport, html

    sequence = [
        html(LOGIN.decode(), headers={"set-cookie": "sid=refused"}),
        html("ordinary response"),
        html("", 302, {"location": "/cgi-bin/login.ha"}),
        html("session refused"),
    ]
    transport = FakeTransport(lambda _request, n: sequence[n - 1])
    client = BGW320Client("http://router.local", access_code="synthetic", transport=transport)
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(RouterAuthError, match="Login failed"):
        session.with_router_session(client, options, client.login)
    assert client.has_authenticated_session() is False
    assert not session.session_paths(client.session_identity()).cache.exists()


def test_page_command_exits_2_when_the_retry_lands_on_login_url(tmp_env, monkeypatch, capsys):
    from save_helpers import client_with

    from bgwcli import cli

    sequence = _login_url_sequence(200)
    client, _transport = client_with(lambda _request, n: sequence[n - 1])
    monkeypatch.setattr(cli, "_client_factory", lambda *_args, **_kwargs: client)
    code = cli.main(["page", "services", "--json"])
    capsys.readouterr()
    assert code == 2
    assert not session.session_paths(client.session_identity()).cache.exists()
