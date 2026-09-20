"""Status view models ported from src/status.ts (device status, home network, security options, status sections)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from bgwcli.status import (
    FALLBACK_DEVICE_STATUS_PAGES,
    FALLBACK_HOME_NETWORK_PAGES,
    FALLBACK_SECURITY_OPTIONS_PAGES,
    FALLBACK_STATUS_PAGES,
    StatusResult,
    StatusSection,
    fetch_device_status,
    fetch_home_network_status,
    fetch_security_options,
    fetch_status_sections,
    parsed_data_count,
)
from bgwcli.types import ParsedField, ParsedPage, to_json_dict

UNUSABLE = "Router returned an unusable response."


def _page(page: str, **kw) -> ParsedPage:
    return ParsedPage(
        page=page, title=kw.pop("title", f"{page} title"), heading=kw.pop("heading", f"{page} heading"), **kw
    )


def _ok(page: str, parsed: ParsedPage, status_code: int | None = 200):
    return SimpleNamespace(page=page, ok=True, status_code=status_code, parsed=parsed, error=None)


def _fail(page: str, error: str | None = None, parsed=None, status_code=None):
    return SimpleNamespace(page=page, ok=False, status_code=status_code, parsed=parsed, error=error)


class _Fetcher:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, client, page, include_secrets=False):
        self.calls.append((page, include_secrets))
        outcome = self.outcomes[page]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fallback_page_lists_match_typescript():
    assert FALLBACK_DEVICE_STATUS_PAGES == ("sysinfo", "broadbandstatistics", "firewall")
    assert FALLBACK_STATUS_PAGES == ("sysinfo", "broadbandstatistics", "fiberstat", "firewall")
    assert FALLBACK_HOME_NETWORK_PAGES == ("etherlan", "dhcpserver", "ipalloc", "wconfig_unified")
    assert FALLBACK_SECURITY_OPTIONS_PAGES == ("firewall", "dosprotect")


def test_parsed_data_count_sums_every_collection():
    empty = _page("home")
    assert parsed_data_count(empty) == 0
    full = _page(
        "home",
        values={"a": "1", "b": "2"},
        tables=[{"x": "y"}],
        fields=[ParsedField(name="f", type="text", value="", checked=False, sensitive=False)],
    )
    assert parsed_data_count(full) == 4


def test_fetch_device_status_uses_home_page_when_it_has_data():
    home = _page("home", values={"Model": "BGW320-505"}, tables=[{"k": "v"}])
    fetcher = _Fetcher({"home": _ok("home", home, 200)})
    result = fetch_device_status(object(), include_secrets=True, fetch_parsed_page=fetcher)
    assert fetcher.calls == [("home", True)]
    assert result == StatusResult(
        page="home",
        fallback=False,
        status_code=200,
        title="home title",
        parsed=home,
        sections=[
            StatusSection(
                page="home",
                ok=True,
                title="home title",
                heading="home heading",
                values={"Model": "BGW320-505"},
                tables=[{"k": "v"}],
            )
        ],
    )
    as_json = to_json_dict(result)
    assert set(as_json) == {"page", "fallback", "statusCode", "title", "parsed", "sections"}
    assert as_json["sections"][0] == {
        "page": "home",
        "ok": True,
        "title": "home title",
        "heading": "home heading",
        "values": {"Model": "BGW320-505"},
        "tables": [{"k": "v"}],
    }


def test_fetch_device_status_omits_status_code_when_fetch_result_lacks_one():
    home = _page("home", values={"a": "b"})
    fetcher = _Fetcher({"home": _ok("home", home, None)})
    result = fetch_device_status(object(), fetch_parsed_page=fetcher)
    assert result.status_code is None
    assert "statusCode" not in to_json_dict(result)


def test_fetch_device_status_falls_back_when_home_is_empty_or_failed():
    empty_home = _page("home")
    sysinfo = _page("sysinfo", values={"Serial": "X"})
    fetcher = _Fetcher(
        {
            "home": _ok("home", empty_home),
            "sysinfo": _ok("sysinfo", sysinfo),
            "broadbandstatistics": _fail("broadbandstatistics", "timeout"),
            "firewall": RouterConnectionError("connection refused"),
        }
    )
    result = fetch_device_status(object(), fetch_parsed_page=fetcher)
    assert [c[0] for c in fetcher.calls] == ["home", "sysinfo", "broadbandstatistics", "firewall"]
    assert result.page == "home"
    assert result.fallback is True
    assert result.parsed is None
    assert result.status_code is None
    assert result.error == UNUSABLE
    assert result.sections == [
        StatusSection(
            page="sysinfo", ok=True, title="sysinfo title", heading="sysinfo heading", values={"Serial": "X"}, tables=[]
        ),
        StatusSection(page="broadbandstatistics", ok=False, values={}, tables=[], error="timeout"),
        StatusSection(page="firewall", ok=False, values={}, tables=[], error="connection refused"),
    ]
    as_json = to_json_dict(result)
    assert set(as_json) == {"page", "fallback", "error", "sections"}
    assert as_json["sections"][1] == {
        "page": "broadbandstatistics",
        "ok": False,
        "values": {},
        "tables": [],
        "error": "timeout",
    }


def test_fetch_device_status_reports_primary_error_when_home_fetch_failed():
    fetcher = _Fetcher(
        {
            "home": _fail("home", "login page"),
            "sysinfo": _fail("sysinfo"),
            "broadbandstatistics": _fail("broadbandstatistics"),
            "firewall": _fail("firewall"),
        }
    )
    result = fetch_device_status(object(), fetch_parsed_page=fetcher)
    assert result.error == "login page"
    assert all(s.error == UNUSABLE for s in result.sections)


def test_fetch_device_status_soft_failure_on_transport_error_but_reraises_auth():
    fetcher = _Fetcher(
        {
            "home": RouterConnectionError("dead"),
            "sysinfo": _fail("sysinfo"),
            "broadbandstatistics": _fail("broadbandstatistics"),
            "firewall": _fail("firewall"),
        }
    )
    result = fetch_device_status(object(), fetch_parsed_page=fetcher)
    assert result.fallback is True
    assert result.error == "dead"

    with pytest.raises(RouterAuthError):
        fetch_device_status(object(), fetch_parsed_page=_Fetcher({"home": RouterAuthError("nope")}))


def test_fetch_home_network_status_primary_and_fallback():
    lan = _page("lanstatistics", tables=[{"Port": "1"}])
    result = fetch_home_network_status(
        object(), fetch_parsed_page=_Fetcher({"lanstatistics": _ok("lanstatistics", lan, 200)})
    )
    assert result.page == "lanstatistics"
    assert result.fallback is False
    assert result.status_code == 200
    assert result.sections[0].page == "lanstatistics"
    assert result.sections[0].tables == [{"Port": "1"}]

    fetcher = _Fetcher(
        {
            "lanstatistics": _fail("lanstatistics", "hang"),
            "etherlan": _ok("etherlan", _page("etherlan", values={"a": "1"})),
            "dhcpserver": _fail("dhcpserver", "x"),
            "ipalloc": _ok("ipalloc", _page("ipalloc")),
            "wconfig_unified": _fail("wconfig_unified"),
        }
    )
    fallback = fetch_home_network_status(object(), fetch_parsed_page=fetcher)
    assert fallback.page == "lanstatistics"
    assert fallback.fallback is True
    assert fallback.error == "hang"
    assert [s.page for s in fallback.sections] == list(FALLBACK_HOME_NETWORK_PAGES)
    assert [s.ok for s in fallback.sections] == [True, False, True, False]
    # An ok fetch with an empty parsed page still counts as an ok section in fallback mode (TS parity).
    assert fallback.sections[2] == StatusSection(
        page="ipalloc", ok=True, title="ipalloc title", heading="ipalloc heading", values={}, tables=[]
    )


def test_fetch_security_options_primary_and_fallback():
    sec = _page("securityoptions", values={"Firewall": "on"})
    result = fetch_security_options(
        object(), fetch_parsed_page=_Fetcher({"securityoptions": _ok("securityoptions", sec, 200)})
    )
    assert result.page == "securityoptions"
    assert result.fallback is False
    assert result.title == "securityoptions title"

    fetcher = _Fetcher(
        {
            "securityoptions": _ok("securityoptions", _page("securityoptions")),
            "firewall": _ok("firewall", _page("firewall", values={"a": "1"})),
            "dosprotect": _fail("dosprotect", "nope"),
        }
    )
    fallback = fetch_security_options(object(), fetch_parsed_page=fetcher)
    assert fallback.fallback is True
    assert fallback.error == UNUSABLE
    assert [s.page for s in fallback.sections] == ["firewall", "dosprotect"]


def test_fetch_status_sections_walks_fixed_page_list():
    fetcher = _Fetcher(
        {
            "sysinfo": _ok("sysinfo", _page("sysinfo", values={"a": "1"})),
            "broadbandstatistics": RouterConnectionError("down"),
            "fiberstat": _fail("fiberstat", "no fiber"),
            "firewall": _ok("firewall", _page("firewall")),
        }
    )
    sections = fetch_status_sections(object(), include_secrets=True, fetch_parsed_page=fetcher)
    assert fetcher.calls == [(p, True) for p in FALLBACK_STATUS_PAGES]
    assert [(s.page, s.ok, s.error) for s in sections] == [
        ("sysinfo", True, None),
        ("broadbandstatistics", False, "down"),
        ("fiberstat", False, "no fiber"),
        ("firewall", True, None),
    ]


def test_fetch_status_sections_reraises_auth_error():
    with pytest.raises(RouterAuthError):
        fetch_status_sections(object(), fetch_parsed_page=_Fetcher({"sysinfo": RouterAuthError("auth")}))


def test_fetch_status_sections_reraises_pool_full_error():
    # TS: RouterSessionPoolFullError extends RouterAuthError, so status.ts:187 rethrows it too.
    with pytest.raises(RouterSessionPoolFullError):
        fetch_status_sections(object(), fetch_parsed_page=_Fetcher({"sysinfo": session_pool_full_error()}))


def test_fetch_device_status_reraises_pool_full_error():
    with pytest.raises(RouterSessionPoolFullError):
        fetch_device_status(object(), fetch_parsed_page=_Fetcher({"home": session_pool_full_error()}))
