"""Shared sweep fakes (not a test module: import from here, never from test_*.py).

Parser/pages/fetch/status/devices are injected through the module-level seams in bgwcli.sweep by
install_backend, which the shared `backend` fixture (tests/conftest.py) calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bgwcli import sweep
from bgwcli.errors import RouterAuthError, RouterSessionPoolFullError
from bgwcli.types import (
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedPage,
    ParsedValueEntry,
)


@dataclass(frozen=True)
class FakeTab:
    section: str
    label: str
    page: str
    dangerous: bool = False


# Mirrors the router-tab order of src/pages.ts for the pages these tests use.
FAKE_TABS = [
    FakeTab("Device", "Status", "home"),
    FakeTab("Device", "Device List", "devices"),
    FakeTab("Device", "Restart Device", "restart", dangerous=True),
    FakeTab("Home Network", "Status", "lanstatistics"),
    FakeTab("Home Network", "Wi-Fi", "wconfig_unified"),
    FakeTab("Home Network", "Subnets & DHCP", "dhcpserver"),
    FakeTab("Firewall", "Security Options", "securityoptions"),
    FakeTab("Diagnostics", "Troubleshoot", "diag"),
    FakeTab("Diagnostics", "Site Map", "sitemap"),
    FakeTab("Diagnostics", "Site Map", "sitemap"),  # duplicate on purpose: sweep must dedupe
]
FAKE_ALIASES = {"troubleshoot": "diag", "wifi": "wconfig_unified"}


@dataclass
class FakeResponse:
    status_code: int
    body: str
    headers: dict = field(default_factory=dict)
    status_message: str = "OK"
    url: str = ""


@dataclass
class FakeClient:
    fail_pages: set[str] = field(default_factory=set)
    auth_error_pages: set[str] = field(default_factory=set)
    forbidden_pages: set[str] = field(default_factory=set)
    login_forbidden_pages: set[str] = field(default_factory=set)
    session_pool_full_pages: set[str] = field(default_factory=set)
    login_pages: set[str] = field(default_factory=set)
    status_codes: dict[str, int] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    kwargs: dict[str, dict] = field(default_factory=dict)

    def get_cgi_page(self, page: str, **kwargs) -> FakeResponse:
        self.calls.append(page)
        self.kwargs[page] = kwargs
        if page in self.fail_pages:
            raise RuntimeError(f"boom {page}")
        if page in self.auth_error_pages:
            raise RouterAuthError("bad authentication")
        if page in self.forbidden_pages:
            # What BGW320Client raises when one page answers HTTP 403: status attached, marked page-level.
            error = RouterAuthError(f"Router rejected https://router/cgi-bin/{page}.ha with HTTP 403.")
            error.status_code = 403
            error.url = f"https://router/cgi-bin/{page}.ha"
            error.page_level = True
            raise error
        if page in self.login_forbidden_pages:
            # What BGW320Client raises when the login handshake itself answers 403: status attached,
            # NOT page-level (a wrong or locked-out access code), so the sweep must abort.
            error = RouterAuthError("Router rejected https://router/cgi-bin/login.ha with HTTP 403.")
            error.status_code = 403
            error.url = "https://router/cgi-bin/login.ha"
            raise error
        if page in self.session_pool_full_pages:
            error = RouterSessionPoolFullError("pool full")
            error.waited_ms = 5
            error.retry_count = 2
            raise error
        title = "Login" if page in self.login_pages else page
        return FakeResponse(
            status_code=self.status_codes.get(page, 200),
            body=f"<title>{title}</title><h1>{page}</h1>\n<form action=\"/cgi-bin/{page}.ha\">"
            '<input name="nonce" value="abc"><input name="target" value="example.com">'
            '<input type="submit" name="Ping" value="Ping"></form>',
            url=f"https://router/cgi-bin/{page}.ha",
        )


def fake_parsed(page: str, title: str | None = None) -> ParsedPage:
    return ParsedPage(
        page=page,
        title=title or page,
        heading=page,
        values={"Status": "Up", "Title": page},
        tables=[],
        fields=[
            ParsedField("nonce", "hidden", "[redacted]", False, True),
            ParsedField("target", "text", "example.com", False, False),
        ],
        buttons=[ParsedButton("Ping", "submit", "Ping", "Ping", False)],
        forms=[ParsedForm("post", f"/cgi-bin/{page}.ha", ["nonce", "target"], [], [], ["Ping"])],
        value_entries=[ParsedValueEntry("", "Status", "Up")],
        links=[],
    )


def fake_parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    title = "Login" if "<title>Login</title>" in html else page
    return fake_parsed(page, title)


def fake_parsed_data_count(parsed: ParsedPage) -> int:
    return (
        len(parsed.values)
        + len(parsed.tables)
        + len(parsed.fields)
        + len(parsed.selects)
        + len(parsed.textareas)
        + len(parsed.buttons)
        + len(parsed.forms)
    )


@dataclass
class FakeParsedPageResult:
    page: str
    ok: bool
    status_code: int | None = None
    parsed: ParsedPage | None = None
    error: str | None = None


def fake_fetch_parsed_page(client, page: str, include_secrets: bool = False) -> FakeParsedPageResult:
    try:
        response = client.get_cgi_page(page)
    except RouterAuthError:
        raise
    except Exception as error:  # noqa: BLE001 - mirrors fetch.ts soft failure
        return FakeParsedPageResult(page=page, ok=False, error=str(error))
    parsed = fake_parse_page(page, response.body, include_secrets)
    if parsed.title == "Login":
        return FakeParsedPageResult(
            page, False, response.status_code, parsed, "Router returned the login page instead of the requested page."
        )
    return FakeParsedPageResult(page, 200 <= response.status_code < 400, response.status_code, parsed)


@dataclass
class FakeStatusSection:
    page: str
    ok: bool
    values: dict[str, str]
    tables: list[dict[str, str]]
    title: str | None = None
    error: str | None = None


@dataclass
class FakeStatusResult:
    page: str
    fallback: bool
    sections: list[FakeStatusSection]
    status_code: int | None = None
    parsed: ParsedPage | None = None
    error: str | None = None


def fake_status_fetcher(page: str, fallback_pages: list[str]):
    def fetch(client, include_secrets: bool = False) -> FakeStatusResult:
        try:
            response = client.get_cgi_page(page)
        except RouterAuthError:
            raise
        except Exception as error:  # noqa: BLE001
            sections = [
                FakeStatusSection(p, True, {"Key": "Value"}, [{"Col": "Row"}]) for p in fallback_pages
            ]
            return FakeStatusResult(page, True, sections, error=str(error))
        parsed = fake_parse_page(page, response.body, include_secrets)
        return FakeStatusResult(page, False, [FakeStatusSection(page, True, parsed.values, parsed.tables)],
                                status_code=response.status_code, parsed=parsed)

    return fetch


@dataclass
class FakeDeviceListResult:
    fallback: bool
    devices: list
    error: str | None = None
    fallback_error: str | None = None


def fake_fetch_device_list(client) -> FakeDeviceListResult:
    client.get_cgi_page("devices")
    return FakeDeviceListResult(False, [object(), object()])


def install_backend(monkeypatch) -> list[int]:
    """Install the fake parser/pages/fetch/status/devices seams and disable sleeping; returns the
    recorded sleeps. The `backend` fixture in conftest.py calls this."""
    sleeps: list[int] = []
    monkeypatch.setattr(sweep, "_router_tabs", lambda: list(FAKE_TABS))
    monkeypatch.setattr(sweep, "_resolve_page", lambda name: FAKE_ALIASES.get(name, name))
    monkeypatch.setattr(sweep, "_parse_page", fake_parse_page)
    monkeypatch.setattr(sweep, "_parsed_data_count", fake_parsed_data_count)
    monkeypatch.setattr(sweep, "_fetch_parsed_page", fake_fetch_parsed_page)
    monkeypatch.setattr(sweep, "_fetch_device_list", fake_fetch_device_list)
    monkeypatch.setattr(
        sweep, "_fetch_device_status", fake_status_fetcher("home", ["sysinfo", "broadbandstatistics", "firewall"])
    )
    monkeypatch.setattr(
        sweep,
        "_fetch_home_network_status",
        fake_status_fetcher("lanstatistics", ["etherlan", "dhcpserver", "ipalloc", "wconfig_unified"]),
    )
    monkeypatch.setattr(
        sweep, "_fetch_security_options", fake_status_fetcher("securityoptions", ["firewall", "dosprotect"])
    )
    monkeypatch.setattr(sweep, "_sleep", lambda ms: sleeps.append(ms))
    return sleeps
