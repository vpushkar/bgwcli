"""Tests for bgwcli.client, ported from BGW320-CLI tests/client.test.ts.

HTTP is never touched: a fake transport is injected. One test drives the real urllib
transport against a local http.server thread to prove cookies / no-redirect handling.
"""

from __future__ import annotations

import hashlib
import http.server
import re
import sys
import threading
import time
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from bgwcli.client import (
    MAX_RESPONSE_BYTES,
    BGW320Client,
    RawResponse,
    RouterResponseError,
    extract_nonce,
    router_sessions_full,
)
from bgwcli.errors import RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from bgwcli.types import HttpResponse, RouterSessionSnapshot

POOL_FULL_HTML = "<title>Login</title><p>all web server sessions are in use</p>"


@pytest.fixture(autouse=True)
def stub_parser(monkeypatch):
    """Deterministic stand-in for bgwcli.parser.looks_like_login (owned by another worker)."""
    module = types.ModuleType("bgwcli.parser")

    def looks_like_login(html: str) -> bool:
        title = re.search(r"<title\b[^>]*>([\s\S]*?)</title>", html, re.I)
        title_text = title.group(1).strip() if title else ""
        return bool(
            re.fullmatch(r"Login", title_text, re.I)
            or re.search(r"Access Code Required", html, re.I)
            or re.search(
                r"<form\b[^>]*action=[\"'][^\"']*/cgi-bin/login\.ha[\"'][^>]*>[\s\S]*id=[\"']password[\"']", html, re.I
            )
        )

    module.looks_like_login = looks_like_login
    module.parse_page = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "bgwcli.parser", module)
    return module


def login_nonce_html(nonce: str) -> str:
    return f'<title>Login</title><form><input name="nonce" value="{nonce}"><input name="password"></form>'


def html(body: str, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    pairs = [("content-type", "text/html")]
    pairs.extend((k, v) for k, v in (headers or {}).items())
    return RawResponse(status=status, reason="OK", headers=pairs, body=body.encode())


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        cookie = request.headers.get("Cookie", "")
        self.calls.append(f"{request.method} {urlsplit(request.url).path} {cookie}")
        return self.handler(request, len(self.calls))


def make_client(transport, **overrides) -> BGW320Client:
    kwargs = dict(
        access_code="12345",
        timeout_ms=1000,
        insecure_tls=True,
        user_agent="test",
        transport=transport,
    )
    kwargs.update(overrides)
    return BGW320Client("http://router.local", **kwargs)


# --- unit helpers -----------------------------------------------------------------


def test_extract_nonce_direct_and_reverse_order():
    assert extract_nonce('<input name="nonce" value="abc123">') == "abc123"
    assert extract_nonce("<input value='deadbeef' type=hidden name='nonce'>") == "deadbeef"
    assert extract_nonce('<input name="nonce" value="XYZ">') is None
    assert extract_nonce("<p>nothing</p>") is None


def test_router_sessions_full_detection():
    assert router_sessions_full(POOL_FULL_HTML)
    assert router_sessions_full("ALL WEB SERVER SESSIONS ARE IN USE")
    assert not router_sessions_full("<title>Login</title>")


def test_origin_normalisation_and_identity():
    assert BGW320Client("192.168.1.254", transport=lambda r: None).session_identity() == "https://192.168.1.254"
    assert BGW320Client("http://Router.local/", transport=lambda r: None).session_identity() == "http://router.local"
    assert BGW320Client("https://10.0.0.1:443", transport=lambda r: None).session_identity() == "https://10.0.0.1"
    assert (
        BGW320Client("http://10.0.0.1:8080///", transport=lambda r: None).session_identity() == "http://10.0.0.1:8080"
    )


# --- login flow -------------------------------------------------------------------


def test_session_pool_full_fails_fast_by_default():
    transport = FakeTransport(lambda req, n: html(POOL_FULL_HTML))
    client = make_client(transport)
    with pytest.raises(RouterSessionPoolFullError):
        client.login()
    assert len(transport.calls) == 1
    assert not client.has_authenticated_session()


def test_wait_for_session_retries_only_pool_full_condition():
    def handler(req, n):
        if n == 1:
            return html(POOL_FULL_HTML)
        if n == 2:
            return html(login_nonce_html("abc123"))
        return html("", status=302, headers={"location": "/cgi-bin/home.ha"})

    events = []
    transport = FakeTransport(handler)
    client = make_client(
        transport,
        wait_for_session=True,
        session_wait_timeout_ms=50,
        session_wait_interval_ms=1,
        on_session_wait=events.append,
    )
    client.login()
    assert [c.rsplit(" ", 1)[0] for c in transport.calls] == [
        "GET /cgi-bin/login.ha",
        "GET /cgi-bin/login.ha",
        "POST /cgi-bin/login.ha",
    ]
    assert len(events) == 1
    assert events[0]["timeoutMs"] == 50 and events[0]["intervalMs"] == 1
    assert client.has_authenticated_session() is False  # 302 but no cookies were set
    assert client.export_session().authenticated is True


def test_wait_for_session_stops_after_timeout_with_retry_metadata():
    transport = FakeTransport(lambda req, n: html(POOL_FULL_HTML))
    client = make_client(transport, wait_for_session=True, session_wait_timeout_ms=5, session_wait_interval_ms=1)
    with pytest.raises(RouterSessionPoolFullError) as info:
        client.login()
    assert info.value.waited_ms == 5
    assert info.value.retry_count > 0
    assert info.value.session_pool_full is True


def test_bad_access_code_does_not_retry_as_pool_full():
    def handler(req, n):
        if req.method == "POST":
            return html("<title>Login</title><p>Login Failed</p>")
        return html(login_nonce_html("abc123"))

    events = []
    transport = FakeTransport(handler)
    client = make_client(
        transport,
        wait_for_session=True,
        session_wait_timeout_ms=20,
        session_wait_interval_ms=1,
        on_session_wait=events.append,
    )
    with pytest.raises(RouterAuthError):
        client.login()
    assert [c.rsplit(" ", 1)[0] for c in transport.calls] == ["GET /cgi-bin/login.ha", "POST /cgi-bin/login.ha"]
    assert events == []


def test_login_posts_md5_of_access_code_plus_nonce_and_masked_password():
    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    make_client(transport, access_code="s3cret").login()
    post = transport.requests[-1]
    fields = parse_qs(post.body.decode(), keep_blank_values=True)
    assert fields["nonce"] == ["abc123"]
    assert fields["password"] == ["******"]
    assert fields["hashpassword"] == [hashlib.md5(b"s3cretabc123").hexdigest()]
    assert fields["Continue"] == ["Continue"]
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert post.headers["Referer"] == "http://router.local/cgi-bin/login.ha"
    assert post.headers["User-Agent"] == "test"


def test_login_requires_access_code():
    transport = FakeTransport(lambda req, n: html(login_nonce_html("abc123")))
    with pytest.raises(RouterAuthError, match="Access code required"):
        make_client(transport, access_code=None).login()
    assert transport.calls == []


def test_login_without_nonce_retries_handshake_then_fails(monkeypatch):
    monkeypatch.setattr("bgwcli.client._sleep", lambda seconds: None)
    transport = FakeTransport(lambda req, n: html("<title>Login</title><p>no nonce here</p>"))
    with pytest.raises(RouterAuthError, match="login nonce"):
        make_client(transport).login()
    assert len(transport.calls) == 16  # 8 attempts x 2 GETs, all GET login.ha


def test_login_is_noop_when_already_authenticated_unless_forced():
    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=abc; Path=/"})
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    client = make_client(transport)
    client.login()
    client.login()
    assert len(transport.calls) == 2
    client.login(force=True)
    assert len(transport.calls) == 4


# --- session snapshots --------------------------------------------------------------


def test_session_snapshots_can_be_reused_without_logging_in_again():
    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=abc; Path=/"})
        if req.url.endswith("/cgi-bin/diag.ha"):
            return html("<title>diag</title>")
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    first = make_client(transport)
    first.login()
    snapshot = first.export_session()
    assert snapshot == RouterSessionSnapshot(origin="http://router.local", authenticated=True, cookies={"sid": "abc"})

    second = make_client(transport)
    second.import_session(snapshot)
    assert second.has_authenticated_session()
    second.get_cgi_page("diag")
    assert transport.calls == [
        "GET /cgi-bin/login.ha ",
        "POST /cgi-bin/login.ha ",
        "GET /cgi-bin/diag.ha sid=abc",
    ]


def test_import_session_ignores_other_origin_or_unauthenticated_snapshots():
    client = make_client(FakeTransport(lambda req, n: html("")))
    client.import_session(RouterSessionSnapshot(origin="http://other.local", authenticated=True, cookies={"sid": "x"}))
    assert not client.has_authenticated_session()
    client.import_session(
        RouterSessionSnapshot(origin="http://router.local", authenticated=False, cookies={"sid": "x"})
    )
    assert not client.has_authenticated_session()
    client.import_session(
        RouterSessionSnapshot(origin="http://router.local", authenticated=True, cookies={"sid": "", "": "v"})
    )
    assert not client.has_authenticated_session()
    client.import_session(RouterSessionSnapshot(origin="http://router.local", authenticated=True, cookies={"sid": "x"}))
    assert client.has_authenticated_session()
    client.clear_session()
    assert not client.has_authenticated_session()
    assert client.export_session().cookies == {}


def test_import_session_accepts_plain_dict_as_written_by_ts_cli():
    client = make_client(FakeTransport(lambda req, n: html("")))
    client.import_session(
        {"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "abc"}, "version": 1}
    )
    assert client.has_authenticated_session()


# --- get / post ----------------------------------------------------------------------


def test_get_cgi_page_logs_in_when_login_page_is_returned():
    state = {"authed": False}

    def handler(req, n):
        if req.method == "POST":
            state["authed"] = True
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "sid=abc"})
        if req.url.endswith("/cgi-bin/diag.ha") and state["authed"]:
            return html("<title>diag</title>")
        return html(login_nonce_html("abc123"))

    transport = FakeTransport(handler)
    response = make_client(transport).get_cgi_page("diag")
    assert isinstance(response, HttpResponse)
    assert response.body == "<title>diag</title>"
    assert [c.rsplit(" ", 1)[0] for c in transport.calls] == [
        "GET /cgi-bin/diag.ha",
        "POST /cgi-bin/login.ha",
        "GET /cgi-bin/diag.ha",
    ]


def test_get_cgi_page_raises_auth_error_when_login_page_persists():
    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(login_nonce_html("abc123"))

    client = make_client(FakeTransport(handler))
    with pytest.raises(RouterAuthError, match="after authentication"):
        client.get_cgi_page("diag")
    assert not client.has_authenticated_session()


def test_get_cgi_page_auth_false_returns_login_page_unchanged():
    transport = FakeTransport(lambda req, n: html(login_nonce_html("abc123")))
    response = make_client(transport).get_cgi_page("sitemap", auth=False)
    assert response.status_code == 200
    assert len(transport.calls) == 1


def test_post_cgi_page_replaces_stale_payload_nonce_with_immediate_page_nonce():
    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html('<title>diag</title><input name="nonce" value="abc123">')

    transport = FakeTransport(handler)
    response = make_client(transport).post_cgi_page("diag", {"nonce": "stale123", "Ping": "Ping"})
    posted = parse_qs(transport.requests[-1].body.decode())
    assert posted["nonce"] == ["abc123"]
    assert posted["Ping"] == ["Ping"]
    assert response.status_code == 302
    assert response.headers["location"] == "/cgi-bin/home.ha"
    assert transport.requests[-1].headers["Referer"] == "http://router.local/cgi-bin/diag.ha"


def _authenticated(transport):
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"SessionID": "ok"}})
    return client


def test_post_cgi_page_rejects_router_error_responses():
    def handler(req, n):
        if req.method == "POST":
            return html("<title>Error</title>", status=500)
        return html('<title>diag</title><input name="nonce" value="fresh123">')

    with pytest.raises(RouterResponseError, match="HTTP 500"):
        _authenticated(FakeTransport(handler)).post_cgi_page("diag", {"Ping": "Ping"})


def test_post_cgi_page_detects_pool_full_and_login_page():
    def pool_full(req, n):
        if req.method == "POST":
            return html(POOL_FULL_HTML)
        return html("<title>diag</title>")

    with pytest.raises(RouterSessionPoolFullError):
        _authenticated(FakeTransport(pool_full)).post_cgi_page("diag", {"Ping": "Ping"})

    def login_page(req, n):
        # Every POST (including the forced re-login retry) is answered with the Login page and the
        # login handshake itself never yields a nonce -> the client gives up with RouterAuthError.
        if req.method == "POST":
            return html("<title>Login</title><form><input id='password'></form>")
        return html("<title>diag</title>")

    with pytest.raises(RouterAuthError):
        _authenticated(FakeTransport(login_page)).post_cgi_page("diag", {"Ping": "Ping"})

def test_redirects_and_response_bodies_are_bounded():
    transport = FakeTransport(lambda req, n: html("", status=302, headers={"location": "/cgi-bin/sitemap.ha"}))
    with pytest.raises(RouterConnectionError, match="Too many redirects"):
        make_client(transport).get_cgi_page("sitemap", auth=False)
    assert len(transport.calls) == 6

    big = FakeTransport(lambda req, n: html("small", headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}))
    with pytest.raises(RouterResponseError, match="exceeded"):
        make_client(big).get_cgi_page("sitemap", auth=False)

    oversized = FakeTransport(lambda req, n: RawResponse(200, "OK", [], b"x" * (MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(RouterResponseError, match="exceeded"):
        make_client(oversized).get_cgi_page("sitemap", auth=False)


def test_get_follows_redirects_relative_to_origin():
    def handler(req, n):
        if n == 1:
            return html("", status=302, headers={"location": "/cgi-bin/home.ha?x=1"})
        return html("<title>Home</title>")

    transport = FakeTransport(handler)
    response = make_client(transport).get_cgi_page("sitemap", auth=False)
    assert response.body == "<title>Home</title>"
    assert transport.requests[1].url == "http://router.local/cgi-bin/home.ha?x=1"
    assert transport.requests[1].method == "GET"


def test_transport_errors_become_connection_errors():
    def boom(req):
        raise OSError("connection refused")

    with pytest.raises(RouterConnectionError, match="connection refused"):
        make_client(boom).get_cgi_page("sitemap", auth=False)

    def slow(req):
        raise TimeoutError()

    with pytest.raises(RouterConnectionError, match="Timed out connecting to http://router.local/cgi-bin/sitemap.ha"):
        make_client(slow).get_cgi_page("sitemap", auth=False)


def test_check_reports_reachability_and_title():
    transport = FakeTransport(lambda req, n: html("<title> Site Map </title>"))
    assert make_client(transport).check() == {
        "host": "router.local",
        "reachable": True,
        "title": "Site Map",
        "authenticated": False,
    }
    down = FakeTransport(lambda req, n: html("", status=503))
    assert make_client(down).check()["reachable"] is False


def test_cookie_header_and_set_cookie_parsing_match_ts_semantics():
    def handler(req, n):
        if n == 1:
            return html(
                "",
                headers={"set-cookie": "a=1; Path=/"},
            )
        return html("")

    transport = FakeTransport(handler)
    client = make_client(transport)
    client.get_cgi_page("x", auth=False)
    # a second Set-Cookie with '=' in the value keeps only the first '=' split, like TS split("=")
    transport.handler = lambda req, n: RawResponse(
        200, "OK", [("set-cookie", "b=2=3; Path=/"), ("set-cookie", "c=; Path=/")], b""
    )
    client.get_cgi_page("x", auth=False)
    client.get_cgi_page("x", auth=False)
    assert transport.requests[-1].headers["Cookie"] == "a=1; b=2"
    assert client.export_session().cookies == {"a": "1", "b": "2"}


def test_from_options_reads_global_options():
    from bgwcli.config import GlobalOptions

    options = GlobalOptions(host="http://router.local", timeout_ms=5, wait_for_session=True, session_wait_timeout_ms=7)
    client = BGW320Client.from_options(options, access_code="1", user_agent="ua", transport=lambda r: None)
    assert client.session_identity() == "http://router.local"
    assert client.options.timeout_ms == 5
    assert client.options.wait_for_session is True
    assert client.options.session_wait_timeout_ms == 7
    assert client.options.user_agent == "ua"


# --- real urllib transport against a local server --------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/cgi-bin/login.ha":
            body = login_nonce_html("abc123").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "SessionID=zzz; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/cgi-bin/diag.ha":
            body = f"<title>diag</title><p>{self.headers.get('Cookie', '')}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        self.server.posts.append((self.path, body, dict(self.headers)))
        self.send_response(302)
        self.send_header("Location", "/cgi-bin/home.ha")
        self.send_header("Set-Cookie", "sid=real; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.posts = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_urllib_transport_login_and_cookies_against_local_server(local_server):
    host, port = local_server.server_address
    client = BGW320Client(f"http://{host}:{port}", access_code="12345", timeout_ms=2000, user_agent="ua")
    client.login()
    assert client.export_session().cookies == {"SessionID": "zzz", "sid": "real"}
    path, body, headers = local_server.posts[0]
    assert path == "/cgi-bin/login.ha"
    assert parse_qs(body)["hashpassword"] == [hashlib.md5(b"12345abc123").hexdigest()]
    assert headers["Cookie"] == "SessionID=zzz"
    assert headers["User-Agent"] == "ua"

    response = client.post_cgi_page("diag", {"Ping": "Ping"})
    assert response.status_code == 302  # redirects are not auto-followed on POST
    assert response.headers["location"] == "/cgi-bin/home.ha"

    page = client.get_cgi_page("diag")
    assert "SessionID=zzz; sid=real" in page.body
    assert client.check()["reachable"] is True


def test_urllib_transport_connection_refused_is_connection_error():
    client = BGW320Client("http://127.0.0.1:9", timeout_ms=500, user_agent="ua")
    with pytest.raises(RouterConnectionError):
        client.get_cgi_page("sitemap", auth=False)


class _DribbleHandler(http.server.BaseHTTPRequestHandler):
    """Sends headers promptly, then one byte every 100 ms for ~5 s: each socket op is fast, but the
    whole response never finishes inside a sane deadline."""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        for _ in range(50):
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.1)

    def log_message(self, *args, **kwargs):  # noqa: D102
        return


def test_urllib_transport_enforces_a_total_deadline_not_just_per_read():
    """TS aborts the whole fetch at timeoutMs (AbortController); a per-socket-op timeout alone would
    let a slow-dripping router hold the CLI for as long as it keeps sending bytes."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DribbleHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = BGW320Client(f"http://127.0.0.1:{server.server_port}", timeout_ms=400)
        started = time.monotonic()
        with pytest.raises(RouterConnectionError, match="[Tt]imed out"):
            client.get_cgi_page("sysinfo", auth=False)
        assert time.monotonic() - started < 2.5
    finally:
        server.shutdown()
        server.server_close()


def test_post_form_takes_the_nonce_from_one_page_and_posts_to_another_cgi_path():
    """home.ha hosts per-radio Restart forms whose action is /cgi-bin/wrestart.ha?1 (2.4 GHz) or ?2
    (5 GHz): the nonce comes from home.ha, the POST goes to the form's own action."""

    def handler(req, n):
        if req.method == "POST":
            assert req.url.endswith("/cgi-bin/wrestart.ha?1")
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        assert req.url.endswith("/cgi-bin/home.ha")
        return html('<title>Status</title><input name="nonce" value="a0c3e5">')

    transport = FakeTransport(handler)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"SessionID": "ok"}})
    response = client.post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    posted = parse_qs(transport.requests[-1].body.decode())
    assert posted == {"nonce": ["a0c3e5"], "WRestart1": ["Restart"]}
    assert transport.requests[-1].headers["Referer"] == "http://router.local/cgi-bin/home.ha"
    assert response.status_code == 302


def test_post_form_uses_the_nonces_of_the_form_whose_action_matches_the_post_path():
    """home.ha serves several forms, each with its own nonce (the Restart forms carry two). The
    first nonce on the page belongs to another form; posting it to wrestart.ha?1 makes the gateway
    answer with the login page (observed live 2026-09-20). Post every nonce of the matching form."""
    home = (
        '<title>Status</title>'
        '<form method="post" action="/cgi-bin/crestart.ha?1"><input type="hidden" name="nonce" value="aaaa01">'
        '<input type="submit" name="Broadband" value="Restart"></form>'
        '<form method="post" action="/cgi-bin/wrestart.ha?1"><input type="hidden" name="nonce" value="bbbb01">'
        '<input type="hidden" name="nonce" value="bbbb02"><input type="submit" name="WRestart1" value="Restart"></form>'
        '<form method="post" action="/cgi-bin/wrestart.ha?2"><input type="hidden" name="nonce" value="cccc01">'
        '<input type="submit" name="WRestart2" value="Restart"></form>'
    )

    def handler(req, n):
        if req.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(home)

    transport = FakeTransport(handler)
    make_client(transport).post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    posted = parse_qs(transport.requests[-1].body.decode())
    assert posted["nonce"] == ["bbbb01", "bbbb02"]
    assert posted["WRestart1"] == ["Restart"]


def test_post_form_logs_in_first_when_the_nonce_page_is_public():
    """home.ha renders without login, so a GET of it never triggers the auto-login; the POST to the
    protected Restart script then answers with the Login page (observed live 2026-09-20). post_form
    must authenticate before posting when no authenticated session exists."""
    home = '<title>Status</title><form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="abc001"><input type="submit" name="WRestart1" value="Restart"></form>'

    def handler(req, n):
        path = urlsplit(req.url).path
        if req.method == "POST" and path == "/cgi-bin/login.ha":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": "SessionID=s1; Path=/"})
        if req.method == "GET" and path == "/cgi-bin/login.ha":
            return html(login_nonce_html("feed01"))
        if req.method == "POST":
            if "SessionID=s1" not in req.headers.get("Cookie", ""):
                return html(login_nonce_html("feed02"))  # unauthenticated -> Login page
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(home)

    transport = FakeTransport(handler)
    client = make_client(transport)
    assert client.has_authenticated_session() is False
    response = client.post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    assert response.status_code == 302
    methods = [f"{r.method} {urlsplit(r.url).path}" for r in transport.requests]
    assert "POST /cgi-bin/login.ha" in methods
    assert methods.index("POST /cgi-bin/login.ha") < methods.index("POST /cgi-bin/wrestart.ha")
    assert client.has_authenticated_session() is True


def test_post_form_relogins_and_retries_once_when_the_post_answers_with_the_login_page():
    """A cached session can be stale: the router answers the POST with the Login page and a fresh
    SessionID. Re-login and retry the POST once instead of failing."""
    home = '<title>Status</title><form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="abc001"><input type="submit" name="WRestart1" value="Restart"></form>'
    state = {"logins": 0}

    def handler(req, n):
        path = urlsplit(req.url).path
        if req.method == "POST" and path == "/cgi-bin/login.ha":
            state["logins"] += 1
            return html("", status=302, headers={"location": "/cgi-bin/home.ha", "set-cookie": f"SessionID=fresh{state['logins']}; Path=/"})
        if req.method == "GET" and path == "/cgi-bin/login.ha":
            return html(login_nonce_html("feed01"))
        if req.method == "POST":
            if "SessionID=fresh" not in req.headers.get("Cookie", ""):
                return html(login_nonce_html("feed02"))
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(home)

    transport = FakeTransport(handler)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"SessionID": "stale"}})
    response = client.post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    assert response.status_code == 302
    assert state["logins"] == 1
    posts = [r for r in transport.requests if r.method == "POST" and urlsplit(r.url).path == "/cgi-bin/wrestart.ha"]
    assert len(posts) == 2  # stale attempt, then the retry with the fresh cookie


def test_slow_pages_get_a_longer_default_timeout_unless_the_timeout_was_set_explicitly():
    """Measured live 2026-09-20: home.ha answers in 17-18 s and lanstatistics.ha in 23-29 s, so the
    15 s default made them fail every time. Per-page floors apply only to the implicit default."""
    from bgwcli.client import SLOW_PAGE_TIMEOUT_MS

    assert SLOW_PAGE_TIMEOUT_MS == {"home": 45000, "lanstatistics": 45000}
    seen = {}

    def handler(req, n):
        seen[urlsplit(req.url).path] = req.timeout_ms
        return html("<title>x</title>")

    make_client(FakeTransport(handler)).get_cgi_page("home", auth=False)
    make_client(FakeTransport(handler)).get_cgi_page("lanstatistics", auth=False)
    make_client(FakeTransport(handler)).get_cgi_page("sysinfo", auth=False)
    assert seen["/cgi-bin/home.ha"] == 45000
    assert seen["/cgi-bin/lanstatistics.ha"] == 45000
    assert seen["/cgi-bin/sysinfo.ha"] == 1000  # make_client's configured 1000 ms is treated as explicit

    seen.clear()
    make_client(FakeTransport(handler), timeout_ms=1000, timeout_explicit=True).get_cgi_page("home", auth=False)
    assert seen["/cgi-bin/home.ha"] == 1000


def test_login_verifies_the_session_when_the_router_does_not_redirect_to_home():
    """A wrong access code does not always come back as a 'Login Failed' page: the gateway can answer
    the login POST with 200 and an ordinary-looking page while leaving the session unauthenticated,
    so the first protected GET returns the login page (observed live 2026-09-20). login() must verify
    the session against a protected page and raise RouterAuthError, so callers such as
    `autorestore`'s fallback access code get their turn and no unauthenticated session is cached."""
    login_html = login_nonce_html("abc123")

    def handler(req, n):
        path = urlsplit(req.url).path
        if req.method == "POST" and path == "/cgi-bin/login.ha":
            return html("<title>Please wait</title><p>Processing your request.</p>", status=200)  # no redirect, not a login page
        if path == "/cgi-bin/login.ha":
            return html(login_html)
        return html(login_html)  # every protected page: still the login page

    client = make_client(FakeTransport(handler))
    with pytest.raises(RouterAuthError):
        client.login()
    assert client.has_authenticated_session() is False


def test_login_accepts_a_200_answer_when_a_protected_page_then_renders():
    def handler(req, n):
        path = urlsplit(req.url).path
        if req.method == "POST" and path == "/cgi-bin/login.ha":
            return html("<title>Welcome</title>", status=200, headers={"set-cookie": "SessionID=ok; Path=/"})
        if path == "/cgi-bin/login.ha":
            return html(login_nonce_html("abc123"))
        return html("<title>Custom Services</title><table></table>")

    client = make_client(FakeTransport(handler))
    client.login()
    assert client.has_authenticated_session() is True
