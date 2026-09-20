"""Sweep engine tests. Parser/pages/fetch/status/devices are injected as fakes through the
module-level seams in bgwcli.sweep so these tests run before those modules exist."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from bgwcli import sweep
from bgwcli.errors import RouterAuthError, RouterSessionPoolFullError, UsageError
from bgwcli.sweep import (
    SweepOptions,
    SweepPage,
    strip_large_payloads,
    sweep_router,
    write_sweep_artifacts,
)
from bgwcli.types import (
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedPage,
    ParsedValueEntry,
    to_json_dict,
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


def fake_fetch_device_list(client) -> FakeDeviceListResult:
    client.get_cgi_page("devices")
    return FakeDeviceListResult(False, [object(), object()])


@pytest.fixture
def backend(monkeypatch):
    """Install the fake parser/pages/fetch/status/devices seams and disable sleeping."""
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


def test_sweep_walks_selected_pages_in_router_tab_order(backend):
    client = FakeClient()
    pages = sweep_router(
        client, SweepOptions(delay_ms=0, pages=["diag", "wconfig_unified", "dhcpserver"], use_fallbacks=False)
    )
    assert [page.page for page in pages] == ["wconfig_unified", "dhcpserver", "diag"]
    assert client.calls == ["wconfig_unified", "dhcpserver", "diag"]


def test_sweep_resolves_aliases_and_dedupes_tabs(backend):
    client = FakeClient()
    pages = sweep_router(client, SweepOptions(pages=[" troubleshoot ", "wifi", "diag"], use_fallbacks=False))
    assert [page.page for page in pages] == ["wconfig_unified", "diag"]

    everything = sweep_router(FakeClient(), SweepOptions(use_fallbacks=False))
    assert [page.page for page in everything].count("sitemap") == 1
    assert len(everything) == len({tab.page for tab in FAKE_TABS})


def test_sweep_rejects_unknown_pages(backend):
    with pytest.raises(UsageError, match="Unknown sweep page\\(s\\): nope"):
        sweep_router(FakeClient(), SweepOptions(pages=["diag", "nope"], use_fallbacks=False))


def test_sweep_continues_after_per_page_failure(backend):
    client = FakeClient(fail_pages={"dhcpserver"})
    pages = sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert [(page.page, page.ok) for page in pages] == [("dhcpserver", False), ("diag", True)]
    assert "boom" in (pages[0].error or "")
    assert pages[0].data_count == 0 and pages[0].data_obtainable is False and pages[0].useful is False


def test_sweep_aborts_immediately_on_authentication_errors(backend):
    client = FakeClient(auth_error_pages={"dhcpserver"})
    with pytest.raises(RouterAuthError):
        sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert client.calls == ["dhcpserver"]


def test_sweep_marks_remaining_pages_skipped_when_session_pool_is_full(backend):
    client = FakeClient(session_pool_full_pages={"dhcpserver"})
    progress = []
    pages = sweep_router(
        client,
        SweepOptions(
            pages=["wconfig_unified", "dhcpserver", "diag"], use_fallbacks=False, on_page_progress=progress.append
        ),
    )
    assert client.calls == ["wconfig_unified", "dhcpserver"]
    assert [(p.page, p.ok, p.skipped, p.session_pool_full) for p in pages] == [
        ("wconfig_unified", True, None, None),
        ("dhcpserver", False, None, True),
        ("diag", False, True, True),
    ]
    assert pages[1].waited_ms == 5 and pages[1].retry_count == 2
    assert pages[2].error == "Skipped because router web session pool is full."
    events = [to_json_dict(event) for event in progress]
    assert {"index": 3, "total": 3, "page": "diag", "phase": "finish", "status": "skipped"} in events
    assert events[0] == {"index": 1, "total": 3, "page": "wconfig_unified", "phase": "start"}
    assert events[1] == {"index": 1, "total": 3, "page": "wconfig_unified", "phase": "finish", "status": "ok"}
    assert events[3] == {"index": 2, "total": 3, "page": "dhcpserver", "phase": "finish", "status": "failed"}


def test_sweep_default_result_is_compact_and_detail_is_opt_in(backend):
    compact = sweep_router(FakeClient(), SweepOptions(pages=["diag"], use_fallbacks=False))
    page = compact[0]
    assert page.page == "diag" and page.ok is True
    assert (page.value_count, page.value_entry_count, page.table_rows) == (2, 1, 0)
    assert (page.field_count, page.button_count, page.form_count, page.link_count) == (2, 1, 1, 0)
    assert (page.select_count, page.textarea_count) == (0, 0)
    assert page.data_count == 6
    assert (page.data_obtainable, page.useful, page.not_only_junk) == (True, True, True)
    assert (page.dangerous, page.guarded, page.status_code) == (False, False, 200)
    assert page.parsed is None and page.raw_html is None and page.controls is None
    as_json = to_json_dict(page)
    assert "parsed" not in as_json and "rawHtml" not in as_json and "controls" not in as_json
    assert as_json["valueCount"] == 2 and as_json["notOnlyJunk"] is True and as_json["statusCode"] == 200

    detailed = sweep_router(
        FakeClient(),
        SweepOptions(pages=["diag"], include_parsed=True, include_forms=True, include_raw=True, use_fallbacks=False),
    )
    assert detailed[0].parsed is not None and detailed[0].parsed.page == "diag"
    assert "<title>diag</title>" in (detailed[0].raw_html or "")
    assert [button.name for button in detailed[0].controls.buttons] == ["Ping"]
    assert to_json_dict(detailed[0])["controls"]["buttons"][0]["name"] == "Ping"


def test_sweep_flags_junk_pages_as_not_ok(backend):
    pages = sweep_router(FakeClient(login_pages={"diag"}), SweepOptions(pages=["diag"], use_fallbacks=False))
    assert pages[0].ok is False and pages[0].title == "Login"
    assert pages[0].data_obtainable is True and pages[0].useful is False and pages[0].not_only_junk is False

    pages = sweep_router(FakeClient(status_codes={"diag": 500}), SweepOptions(pages=["diag"], use_fallbacks=False))
    assert pages[0].ok is False and pages[0].status_code == 500


def test_sweep_fetches_sitemap_without_auth(backend):
    client = FakeClient()
    sweep_router(client, SweepOptions(pages=["sitemap", "diag"], use_fallbacks=False))
    assert client.kwargs["sitemap"] == {"auth": False}
    assert client.kwargs["diag"] == {}


def test_sweep_marks_dangerous_tabs_guarded(backend):
    pages = sweep_router(FakeClient(), SweepOptions(pages=["restart"], use_fallbacks=False))
    assert pages[0].dangerous is True and pages[0].guarded is True
    assert (pages[0].section, pages[0].label) == ("Device", "Restart Device")


def test_sweep_sleeps_between_pages_when_delay_set(backend):
    sweep_router(FakeClient(), SweepOptions(delay_ms=250, pages=["diag", "dhcpserver"], use_fallbacks=False))
    assert backend == [250, 250]
    backend.clear()
    sweep_router(FakeClient(), SweepOptions(delay_ms=0, pages=["diag"], use_fallbacks=False))
    assert backend == []


def test_sweep_exposes_fallback_section_data_only_with_parsed_detail(backend):
    compact = sweep_router(FakeClient(fail_pages={"home"}), SweepOptions(pages=["home"]))
    assert (compact[0].page, compact[0].ok, compact[0].fallback) == ("home", True, True)
    assert compact[0].title == "Device Status fallback"
    assert compact[0].fallback_sections is None
    assert compact[0].value_count == 3 and compact[0].table_rows == 3 and compact[0].data_count == 6
    assert "boom" in (compact[0].error or "")

    detailed = sweep_router(FakeClient(fail_pages={"home"}), SweepOptions(pages=["home"], include_parsed=True))
    assert [section.page for section in detailed[0].fallback_sections] == ["sysinfo", "broadbandstatistics", "firewall"]
    assert all(section.values for section in detailed[0].fallback_sections)
    assert to_json_dict(detailed[0])["fallbackSections"][0]["page"] == "sysinfo"


def test_sweep_uses_status_views_when_pages_load_directly(backend):
    client = FakeClient()
    pages = sweep_router(client, SweepOptions(pages=["home", "lanstatistics", "securityoptions"]))
    assert [(p.page, p.ok, p.fallback) for p in pages] == [
        ("home", True, None),
        ("lanstatistics", True, None),
        ("securityoptions", True, None),
    ]
    assert pages[0].title == "home" and pages[0].data_count == 6

    lan_fallback = sweep_router(FakeClient(fail_pages={"lanstatistics"}), SweepOptions(pages=["lanstatistics"]))
    assert lan_fallback[0].title == "Home Network Status fallback" and lan_fallback[0].fallback is True
    sec_fallback = sweep_router(FakeClient(fail_pages={"securityoptions"}), SweepOptions(pages=["securityoptions"]))
    assert sec_fallback[0].title == "Security Options fallback" and sec_fallback[0].fallback is True


def test_sweep_devices_fallback_counts_devices(backend):
    compact = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))
    page = compact[0]
    assert (page.ok, page.fallback, page.title) == (True, False, "Device List")
    assert page.table_rows == 2 and page.data_count == 2 and page.devices is None
    detailed = sweep_router(FakeClient(), SweepOptions(pages=["devices"], include_parsed=True))
    assert len(detailed[0].devices) == 2


def test_sweep_generic_fallback_path_reports_unusable_pages(backend):
    login = sweep_router(FakeClient(login_pages={"diag"}), SweepOptions(pages=["diag"]))
    assert login[0].ok is False
    assert login[0].error == "Router returned the login page instead of the requested page."
    assert login[0].data_count == 6  # parsed page still counted, like TS

    broken = sweep_router(FakeClient(fail_pages={"diag"}), SweepOptions(pages=["diag"]))
    assert broken[0].ok is False and "boom" in broken[0].error and broken[0].data_count == 0


def test_sweep_raw_disables_fallbacks(backend):
    client = FakeClient(fail_pages={"home"})
    pages = sweep_router(client, SweepOptions(pages=["home"], include_raw=True))
    assert pages[0].ok is False and pages[0].fallback is None
    assert client.calls == ["home"]


def test_strip_large_payloads_drops_raw_parsed_and_controls(backend):
    detailed = sweep_router(
        FakeClient(),
        SweepOptions(pages=["diag"], include_parsed=True, include_forms=True, include_raw=True, use_fallbacks=False),
    )[0]
    compact = strip_large_payloads(detailed)
    assert isinstance(compact, SweepPage)
    assert compact.parsed is None and compact.raw_html is None and compact.controls is None
    assert compact.value_count == detailed.value_count and compact.page == "diag"
    assert detailed.parsed is not None  # original untouched


def test_write_sweep_artifacts_writes_html_parsed_and_compact_sweep_json(backend, tmp_path):
    pages = sweep_router(
        FakeClient(fail_pages={"dhcpserver"}),
        SweepOptions(pages=["diag", "dhcpserver"], include_parsed=True, include_raw=True, use_fallbacks=False),
    )
    out = tmp_path / "out"
    written = write_sweep_artifacts(pages, out)

    html_path = out / "router-html" / "diag.html"
    parsed_path = out / "parsed" / "diag.json"
    assert html_path.read_text().endswith("</form>\n")
    assert "<title>diag</title>" in html_path.read_text()
    parsed_json = json.loads(parsed_path.read_text())
    assert parsed_json["page"] == "diag" and parsed_json["valueEntries"][0]["label"] == "Status"
    assert not (out / "router-html" / "dhcpserver.html").exists()
    assert not (out / "parsed" / "dhcpserver.json").exists()

    by_page = {page.page: page for page in written}
    assert by_page["diag"].artifacts.html == str(html_path) and by_page["diag"].artifacts.parsed == str(parsed_path)
    assert by_page["diag"].raw_html is None and by_page["diag"].parsed is None
    assert by_page["dhcpserver"].artifacts is None

    sweep_json = json.loads((out / "sweep.json").read_text())
    assert [entry["page"] for entry in sweep_json] == ["dhcpserver", "diag"]  # router-tab order
    entries = {entry["page"]: entry for entry in sweep_json}
    assert entries["diag"]["artifacts"] == {"html": str(html_path), "parsed": str(parsed_path)}
    assert "rawHtml" not in entries["diag"] and "parsed" not in entries["diag"]
    assert "artifacts" not in entries["dhcpserver"]


def test_sweep_page_json_keys_are_camel_case(backend):
    client = FakeClient(session_pool_full_pages={"diag"})
    page = sweep_router(client, SweepOptions(pages=["diag"], use_fallbacks=False))[0]
    as_json = to_json_dict(page)
    assert set(as_json) >= {
        "section", "label", "page", "dangerous", "guarded", "ok", "error", "valueCount", "tableRows",
        "fieldCount", "selectCount", "textareaCount", "buttonCount", "formCount", "dataCount",
        "dataObtainable", "useful", "notOnlyJunk", "sessionPoolFull", "waitedMs", "retryCount",
    }
    assert "skipped" not in as_json and "statusCode" not in as_json
