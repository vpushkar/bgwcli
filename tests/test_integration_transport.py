"""Integration: real HTTP -> BGW320Client -> fetch -> real parser -> snapshot -> restore.

A local threading http.server mimics the gateway closely enough for login (nonce + md5 hash, 302 to
/cgi-bin/home.ha), authenticated page serving, POSTs that answer 302 + Location, and a "Page not
found" page. No module is stubbed: the client uses its real urllib transport and the real parser.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from urllib.parse import parse_qs

import pytest
from integration_html import APPHOSTING_HTML, SERVICES_HTML, SYSINFO_HTML

from bgwcli.client import BGW320Client
from bgwcli.errors import RouterAuthError
from bgwcli.fetch import LOGIN_PAGE_ERROR, fetch_parsed_page
from bgwcli.restore import RestoreOptions, build_restore_plan, execute_restore
from bgwcli.session import SessionCoordinatorOptions, session_paths, with_router_session
from bgwcli.snapshot import SnapshotService, extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

ACCESS_CODE = "0123456789"
LOGIN_NONCE = "a1b2c3d4e5f6"
AUTH_COOKIE = "auth=ok"

LOGIN_HTML = f"""<html><head><title>Login</title></head><body>
<form method="post" action="/cgi-bin/login.ha"><input type="hidden" name="nonce" value="{LOGIN_NONCE}">
<input type="password" id="password" name="password"><input type="submit" name="Continue" value="Continue"></form>
</body></html>"""

LOGIN_FAILED_HTML = LOGIN_HTML.replace("<body>", "<body><p>Login Failed</p>")
NOT_FOUND_HTML = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"
HOME_HTML = "<html><head><title>Home</title></head><body><h1>Device</h1></body></html>"

PAGES = {"services": SERVICES_HTML, "apphosting": APPHOSTING_HTML, "sysinfo": SYSINFO_HTML, "home": HOME_HTML}


class _Gateway(http.server.BaseHTTPRequestHandler):
    server: _GatewayServer

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticated(self) -> bool:
        return AUTH_COOKIE in self.headers.get("Cookie", "")

    def _page(self) -> str:
        return self.path.removeprefix("/cgi-bin/").removesuffix(".ha")

    def do_GET(self):
        self.server.gets.append(self.path)
        if self.path == "/cgi-bin/login.ha" or not self._authenticated():
            # The gateway answers any unauthenticated page with its login page and hands out the
            # session cookie with it; the client takes the nonce from that page without a login.ha GET.
            self._send(200, LOGIN_HTML, {"Set-Cookie": "SessionID=s1; Path=/"})
            return
        page = self._page()
        if page in PAGES:
            self._send(200, PAGES[page])
        else:
            self._send(200, NOT_FOUND_HTML)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        fields = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        self.server.posts.append((self.path, fields, dict(self.headers)))
        if self.path == "/cgi-bin/login.ha":
            expected = hashlib.md5(f"{ACCESS_CODE}{fields.get('nonce', '')}".encode()).hexdigest()
            if fields.get("hashpassword") == expected:
                self._send(302, "", {"Location": "/cgi-bin/home.ha", "Set-Cookie": f"{AUTH_COOKIE}; Path=/"})
            else:
                self._send(200, LOGIN_FAILED_HTML)
            return
        if not self._authenticated():
            self._send(200, LOGIN_HTML)
            return
        self._send(302, "", {"Location": self.path})


class _GatewayServer(http.server.ThreadingHTTPServer):
    def __init__(self, address):
        super().__init__(address, _Gateway)
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, str], dict[str, str]]] = []

    @property
    def login_posts(self) -> list[dict[str, str]]:
        return [fields for path, fields, _ in self.posts if path == "/cgi-bin/login.ha"]


@pytest.fixture
def gateway():
    server = _GatewayServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def make_client(gateway: _GatewayServer, access_code: str | None = ACCESS_CODE) -> BGW320Client:
    host, port = gateway.server_address
    return BGW320Client(f"http://{host}:{port}", access_code=access_code, timeout_ms=3000, user_agent="bgwcli-test")


def test_login_then_fetch_parsed_page_runs_the_real_parser_over_the_wire(gateway):
    client = make_client(gateway)
    client.login()
    assert client.has_authenticated_session() is True
    assert gateway.login_posts[0]["hashpassword"] == hashlib.md5(f"{ACCESS_CODE}{LOGIN_NONCE}".encode()).hexdigest()
    assert gateway.login_posts[0]["password"] == "*" * len(ACCESS_CODE)

    result = fetch_parsed_page(client, "services", include_secrets=True)
    assert result.ok is True and result.status_code == 200 and result.error is None
    assert result.parsed is not None
    assert result.parsed.title == "Custom Services"
    assert [b.name for b in result.parsed.buttons] == ["Remove_1", "Remove_2", "Add"]
    snap = extract_snapshot({"services": result.parsed}, ts="t", router_host="r")
    assert snap.services == [
        SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
        SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
    ]


def test_get_cgi_page_logs_in_transparently_when_the_gateway_answers_with_its_login_page(gateway):
    client = make_client(gateway)
    response = client.get_cgi_page("sysinfo")
    assert "System Information" in response.body
    assert len(gateway.login_posts) == 1
    # The unauthenticated GET, the login-page GET is skipped (nonce taken from the served login page), then the retry.
    assert gateway.gets == ["/cgi-bin/sysinfo.ha", "/cgi-bin/sysinfo.ha"]


def test_wrong_access_code_raises_router_auth_error_and_fetch_propagates_it(gateway):
    client = make_client(gateway, access_code="wrong")
    with pytest.raises(RouterAuthError, match="Login failed"):
        client.login()
    with pytest.raises(RouterAuthError):
        fetch_parsed_page(client, "services")


def test_fetch_parsed_page_reports_a_page_not_found_page_as_unavailable(gateway):
    client = make_client(gateway)
    client.login()
    result = fetch_parsed_page(client, "nosuchpage")
    assert result.ok is False
    assert result.status_code == 200
    assert result.error == "Page not found"


def test_fetch_parsed_page_flags_a_leaked_login_page_without_access_code(gateway):
    # No access code: the client cannot log in, so the login page leaks through as an auth error.
    client = make_client(gateway, access_code=None)
    with pytest.raises(RouterAuthError, match="Access code required"):
        fetch_parsed_page(client, "services")
    # With auth=False the page is returned as-is and the parser recognises it as the login page.
    from bgwcli.parser import looks_like_login

    raw = client.get_cgi_page("services", auth=False)
    assert looks_like_login(raw.body) is True
    assert LOGIN_PAGE_ERROR.startswith("Router returned the login page")


def test_post_cgi_page_carries_the_served_nonce_and_returns_the_302_location_unfollowed(gateway):
    client = make_client(gateway)
    client.login()
    response = client.post_cgi_page("services", {"Service": "Extra", "Add": "Add", "nonce": "stale"})
    assert response.status_code == 302
    assert response.headers["location"] == "/cgi-bin/services.ha"
    path, fields, headers = gateway.posts[-1]
    assert path == "/cgi-bin/services.ha"
    assert fields == {"Service": "Extra", "Add": "Add", "nonce": "abc"}  # page nonce replaces the stale one
    assert headers["Referer"].endswith("/cgi-bin/services.ha")
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert AUTH_COOKIE in headers["Cookie"]


def test_restore_plan_from_live_pages_executes_over_http_and_records_locations(gateway):
    client = make_client(gateway)
    pages = {}
    for page in ("services", "apphosting"):
        result = fetch_parsed_page(client, page, include_secrets=True)
        assert result.ok and result.parsed is not None
        pages[page] = result.parsed
    live = extract_snapshot(pages, ts="t", router_host="r")
    from dataclasses import replace

    wanted = replace(live, services=[*live.services, SnapshotService("Extra", 7000, 7000, 7000, "UDP")])
    options = RestoreOptions()
    steps = build_restore_plan(diff_snapshots(wanted, live), wanted, pages, options)
    assert [f"{s.page}:{s.kind}" for s in steps] == ["services:add-service", "packetfilter:skip"]

    execution = execute_restore(client, steps)
    assert [s.status for s in execution.steps] == ["applied", "skipped"]
    assert execution.steps[0].status_code == 302
    assert execution.steps[0].location == "/cgi-bin/services.ha"
    path, fields, _ = gateway.posts[-1]
    assert path == "/cgi-bin/services.ha"
    assert (
        fields.items()
        >= {"Service": "Extra", "extMinPort": "7000", "protocol": "UDP", "Add": "Add", "nonce": "abc"}.items()
    )
    assert len(gateway.login_posts) == 1


def test_with_router_session_writes_the_cache_and_a_second_client_reuses_it_without_logging_in(gateway, tmp_env):
    options = SessionCoordinatorOptions(
        cache_ttl_ms=120000, pool_cooldown_ms=300000, lock_timeout_ms=2000, wait_for_session=False
    )

    first = make_client(gateway)
    result = with_router_session(first, options, lambda: fetch_parsed_page(first, "services", include_secrets=True))
    assert result.ok is True
    assert len(gateway.login_posts) == 1

    paths = session_paths(first.session_identity())
    assert paths.cache.parent == tmp_env / "cache"
    assert paths.cache.exists() and not paths.lock.exists()
    assert (paths.cache.stat().st_mode & 0o777) == 0o600
    cached = json.loads(paths.cache.read_text())
    assert cached["origin"] == first.session_identity()
    assert cached["authenticated"] is True
    assert cached["cookies"] == {"SessionID": "s1", "auth": "ok"}
    assert cached["version"] == 1 and cached["expiresAt"] > cached["cachedAt"]

    second = make_client(gateway, access_code=None)  # no access code: could not log in on its own
    result2 = with_router_session(second, options, lambda: fetch_parsed_page(second, "sysinfo", include_secrets=True))
    assert result2.ok is True and result2.parsed is not None
    assert result2.parsed.values == {"Software Version": "4.27.7"}
    assert second.has_authenticated_session() is True
    assert len(gateway.login_posts) == 1  # the cached cookies carried the session over
    assert not paths.lock.exists()
