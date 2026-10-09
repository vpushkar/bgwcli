"""Shared fakes for save-path tests (not a test module: import from here, never from test_*.py).

Runs the real BGW320Client over an in-memory transport so the parsers, nonce reads and the
acknowledgement wait are exercised; only the wire is synthetic.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from bgwcli import client as client_module
from bgwcli import restore
from bgwcli.client import BGW320Client, RawResponse

# Banner texts exactly as the gateway renders them (live evidence 2026-10-06 for the no-op Wi-Fi save).
SAVED_RED = '<div id="error-message-text" style="color: red">Changes saved</div>'
NO_CHANGE = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">No changes detected. Save not performed.</div>'
)
ERROR = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">A required setting is empty</div>'
)


def html(body: str, status: int = 200, headers: dict[str, str] | None = None) -> RawResponse:
    pairs = [("content-type", "text/html")]
    pairs.extend((k, v) for k, v in (headers or {}).items())
    return RawResponse(status=status, reason="OK", headers=pairs, body=body.encode())


def form(page: str, value: str, name: str = "setting", banner: str = "") -> str:
    return (
        f'{banner}<form action="/cgi-bin/{page}.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="{name}" value="{value}">'
        '<input type="submit" name="Save" value="Save"></form>'
    )


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


def client_with(handler) -> tuple[BGW320Client, FakeTransport]:
    """An authenticated client whose transport answers `handler(request, request_number)`."""
    transport = FakeTransport(handler)
    client = BGW320Client(
        "http://router.local", access_code="12345", timeout_ms=1000, insecure_tls=True,
        user_agent="test", transport=transport,
    )
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "test"}})
    return client, transport


class FakeClock:
    """Replaces restore's monotonic/sleep so acknowledgement polling finishes instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.login_sleeps: list[float] = []  # client login-retry backoff pauses, recorded and never slept

    def login_sleep(self, seconds: float) -> None:
        self.login_sleeps.append(seconds)
        self.now += seconds  # the fake clock moves on, like the save-wait sleeps do

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


def install_clock(monkeypatch, *, timeout: float = 3.0, poll: float = 1.0) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(restore, "monotonic", fake.monotonic, raising=False)
    monkeypatch.setattr(restore, "sleep", fake.sleep, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_TIMEOUT_SECONDS", timeout, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_POLL_SECONDS", poll, raising=False)
    monkeypatch.setattr(client_module, "_sleep", fake.login_sleep)
    # The client's pool-wait loop measures elapsed time with its own clock; keep it on the fake one too,
    # so a `--wait-for-session` test under this fixture advances through its recorded sleeps instead of
    # spinning against real time while the sleeps are silenced.
    monkeypatch.setattr(client_module, "_now_ms", lambda: int(fake.now * 1000), raising=False)
    return fake
