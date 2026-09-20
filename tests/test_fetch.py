"""Tests for bgwcli.fetch (fetch_parsed_page / parsed_data_count), ported from the fetch cases in
BGW320-CLI tests/audit.test.ts. The parser is stubbed: parse_page is owned by another worker."""

from __future__ import annotations

import re
import sys
import types
from dataclasses import fields

import pytest

from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from bgwcli.fetch import ParsedPageResult, fetch_parsed_page, parsed_data_count
from bgwcli.types import HttpResponse, ParsedButton, ParsedField, ParsedForm, ParsedPage, ParsedSelect, ParsedTextarea


@pytest.fixture(autouse=True)
def stub_parser(monkeypatch):
    module = types.ModuleType("bgwcli.parser")
    calls = []

    def title_of(html: str) -> str:
        match = re.search(r"<title\b[^>]*>([\s\S]*?)</title>", html, re.I)
        return match.group(1).strip() if match else ""

    def heading_of(html: str) -> str:
        match = re.search(r"<h1\b[^>]*>([\s\S]*?)</h1>", html, re.I)
        return match.group(1).strip() if match else ""

    def parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
        calls.append((page, include_secrets))
        return ParsedPage(page=page, title=title_of(html), heading=heading_of(html), values={"x": "1"})

    def looks_like_login(html: str) -> bool:
        return title_of(html).lower() == "login" or "Access Code Required" in html

    module.parse_page = parse_page
    module.looks_like_login = looks_like_login
    module.calls = calls
    monkeypatch.setitem(sys.modules, "bgwcli.parser", module)
    return module


class FakeClient:
    def __init__(self, body: str = "", status: int = 200, error: Exception | None = None):
        self.body, self.status, self.error = body, status, error
        self.pages: list[str] = []

    def get_cgi_page(self, page: str, *, auth: bool = True) -> HttpResponse:
        self.pages.append(page)
        if self.error:
            raise self.error
        return HttpResponse(self.status, "OK", {}, self.body, f"https://router/cgi-bin/{page}.ha")


def test_parsed_page_result_shape():
    names = [f.name for f in fields(ParsedPageResult)]
    assert names == ["page", "ok", "status_code", "parsed", "error"]
    assert ParsedPageResult(page="home", ok=True).status_code is None


def test_fetch_parsed_page_returns_parsed_page_on_success(stub_parser):
    client = FakeClient("<title>Home</title><h1>Device Status</h1>")
    result = fetch_parsed_page(client, "home")
    assert result.page == "home" and result.ok is True and result.status_code == 200 and result.error is None
    assert result.parsed.title == "Home" and result.parsed.heading == "Device Status"
    assert client.pages == ["home"]
    assert stub_parser.calls == [("home", False)]


def test_fetch_parsed_page_passes_include_secrets(stub_parser):
    fetch_parsed_page(FakeClient("<title>x</title>"), "wifi", include_secrets=True)
    assert stub_parser.calls == [("wifi", True)]


def test_fetch_parsed_page_treats_page_not_found_html_as_unavailable():
    client = FakeClient("<title>Page not found</title><h1>Page not found.</h1>")
    result = fetch_parsed_page(client, "securityoptions")
    assert (result.page, result.ok, result.status_code, result.error) == (
        "securityoptions",
        False,
        200,
        "Page not found.",
    )
    assert result.parsed is not None

    only_title = fetch_parsed_page(FakeClient("<title>Page not found</title>"), "x")
    assert only_title.ok is False and only_title.error == "Page not found"

    similar = fetch_parsed_page(FakeClient("<title>Page not found in menu</title>"), "x")
    assert similar.ok is True


def test_fetch_parsed_page_treats_leaked_login_html_as_unavailable():
    client = FakeClient('<title>Login</title><form><input id="password" name="password"></form>')
    result = fetch_parsed_page(client, "broadbandconfig")
    assert result.page == "broadbandconfig"
    assert result.ok is False
    assert result.status_code == 200
    assert result.error == "Router returned the login page instead of the requested page."
    assert result.parsed is not None


def test_fetch_parsed_page_ok_follows_status_code_range():
    assert fetch_parsed_page(FakeClient("<title>a</title>", status=399), "a").ok is True
    result = fetch_parsed_page(FakeClient("<title>a</title>", status=404), "a")
    assert result.ok is False and result.status_code == 404 and result.error is None
    assert fetch_parsed_page(FakeClient("<title>a</title>", status=199), "a").ok is False


def test_fetch_parsed_page_reports_connection_errors_but_rethrows_auth_errors():
    result = fetch_parsed_page(FakeClient(error=RouterConnectionError("Timed out connecting to x")), "home")
    assert result == ParsedPageResult(page="home", ok=False, error="Timed out connecting to x")

    generic = fetch_parsed_page(FakeClient(error=ValueError("odd")), "home")
    assert generic.ok is False and generic.error == "odd"

    with pytest.raises(RouterAuthError):
        fetch_parsed_page(FakeClient(error=RouterAuthError("Login failed.")), "home")
    with pytest.raises(RouterSessionPoolFullError):
        fetch_parsed_page(FakeClient(error=session_pool_full_error()), "home")


def test_parsed_data_count_sums_every_bucket():
    page = ParsedPage(
        page="p",
        title="",
        heading="",
        values={"a": "1", "b": "2"},
        tables=[{"r": "1"}],
        fields=[ParsedField("f", "text", "", False, False)] * 3,
        selects=[ParsedSelect("s", "", [], False)],
        textareas=[ParsedTextarea("t", "", False)] * 2,
        buttons=[ParsedButton("b", "submit", "", "", False)],
        forms=[ParsedForm("post", "/x", [], [], [], [])] * 2,
        value_entries=[],
        links=[],
    )
    assert parsed_data_count(page) == 2 + 1 + 3 + 1 + 2 + 1 + 2
    assert parsed_data_count(ParsedPage(page="p", title="", heading="")) == 0
