"""Ported from tests/devices.test.ts plus fetch_device_list fallback behavior from src/devices.ts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bgwcli.client import session_pool_full_error
from bgwcli.devices import (
    DeviceListResult,
    devices_from_ip_allocation_rows,
    fetch_device_list,
    filter_devices,
    split_ip_and_name,
)
from bgwcli.errors import RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from bgwcli.types import Device, ParsedPage, to_json_dict


def test_devices_from_ip_allocation_rows_builds_degraded_device_records():
    rows = [
        {
            "IPv4 Address / Name": "192.168.1.20 / laptop",
            "MAC Address": "aa:bb:cc:dd:ee:ff",
            "Status": "on",
            "Allocation": "dhcp",
        }
    ]
    devices = devices_from_ip_allocation_rows(rows)
    assert devices == [
        Device(
            status="on",
            name="laptop",
            ip="192.168.1.20",
            mac="aa:bb:cc:dd:ee:ff",
            connection="",
            allocation="dhcp",
            last_activity="",
        )
    ]
    assert to_json_dict(devices) == [
        {
            "status": "on",
            "name": "laptop",
            "ip": "192.168.1.20",
            "mac": "aa:bb:cc:dd:ee:ff",
            "connection": "",
            "allocation": "dhcp",
            "lastActivity": "",
        }
    ]


def test_devices_from_ip_allocation_rows_drops_empty_rows_and_tolerates_missing_columns():
    rows = [
        {"Status": "off"},
        {"MAC Address": "11:22:33:44:55:66"},
        {"IPv4 Address / Name": "10.0.0.5"},
    ]
    devices = devices_from_ip_allocation_rows(rows)
    assert [d.mac for d in devices] == ["11:22:33:44:55:66", ""]
    assert devices[1].ip == "10.0.0.5"
    assert devices[1].name == ""
    assert devices[1].allocation is None


def test_split_ip_and_name_only_splits_on_slash_with_surrounding_whitespace():
    assert split_ip_and_name("192.168.1.2 / host") == ("192.168.1.2", "host")
    assert split_ip_and_name("192.168.1.2/host") == ("", "192.168.1.2/host")
    assert split_ip_and_name("") == ("", "")
    assert split_ip_and_name("1.2.3.4 / a / b") == ("1.2.3.4", "a / b")
    assert split_ip_and_name("1.2.3.4 /") == ("1.2.3.4", "")


def _dev(status: str, mac: str = "00:00:00:00:00:01") -> Device:
    return Device(status=status, name="n", ip="", mac=mac, connection="")


def test_filter_devices_keeps_only_online_unless_include_offline():
    devices = [_dev("on"), _dev("off"), _dev(" ON "), _dev("Online"), _dev("Disconnected"), _dev("")]
    # The router reports exactly "on"/"off"; a status merely containing the letters is not online.
    assert [d.status for d in filter_devices(devices, include_offline=False)] == ["on", " ON "]
    assert filter_devices(devices, include_offline=True) == devices


_DEVICE_TABLE = (
    "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>MAC Address</th><th>Connection</th></tr></table>"
)


class _Response:
    def __init__(self, body: str, status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class _Client:
    def __init__(self, get):
        self._get = get
        self.calls: list[str] = []

    def get_cgi_page(self, page: str):
        self.calls.append(page)
        return self._get(page)


def test_fetch_device_list_uses_devices_page_and_injected_parser():
    client = _Client(lambda page: _Response(_DEVICE_TABLE))
    parsed = [_dev("on"), _dev("off")]
    result = fetch_device_list(client, include_offline=False, parse_devices=lambda html: parsed)
    assert result == DeviceListResult(fallback=False, devices=[parsed[0]])
    assert client.calls == ["devices"]
    assert to_json_dict(result) == {
        "fallback": False,
        "devices": [{"status": "on", "name": "n", "ip": "", "mac": "00:00:00:00:00:01", "connection": ""}],
    }


def test_fetch_device_list_include_offline_keeps_all():
    client = _Client(lambda page: _Response(_DEVICE_TABLE))
    parsed = [_dev("on"), _dev("off")]
    result = fetch_device_list(client, include_offline=True, parse_devices=lambda html: parsed)
    assert result.devices == parsed


def test_fetch_device_list_reraises_auth_errors():
    def get(page):
        raise RouterAuthError("login failed")

    with pytest.raises(RouterAuthError):
        fetch_device_list(_Client(get), parse_devices=lambda html: [])


def test_fetch_device_list_falls_back_to_ipalloc_rows_on_transport_error():
    def get(page):
        raise RouterConnectionError("devices.ha timed out")

    ipalloc = ParsedPage(
        page="ipalloc",
        title="IP Allocation",
        heading="IP Allocation",
        tables=[
            {
                "IPv4 Address / Name": "192.168.1.20 / laptop",
                "MAC Address": "aa:bb",
                "Status": "on",
                "Allocation": "dhcp",
            },
            {"IPv4 Address / Name": "192.168.1.21 / tv", "MAC Address": "cc:dd", "Status": "off", "Allocation": "dhcp"},
        ],
    )

    def fetch(client, page, include_secrets=False):
        assert page == "ipalloc"
        return SimpleNamespace(page=page, ok=True, status_code=200, parsed=ipalloc, error=None)

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert result.fallback is True
    assert result.error == "devices.ha timed out"
    assert [d.name for d in result.devices] == ["laptop"]

    all_result = fetch_device_list(
        _Client(get), include_offline=True, parse_devices=lambda html: [], fetch_parsed_page=fetch
    )
    assert [d.name for d in all_result.devices] == ["laptop", "tv"]


def test_fetch_device_list_fallback_failure_yields_empty_list_with_original_error():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        return SimpleNamespace(page=page, ok=False, status_code=None, parsed=None, error="ipalloc failed")

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    # Nothing could be read: the caller exits 2, the devices.ha error is kept.
    assert result == DeviceListResult(fallback=True, devices=[], error="boom", fallback_error="ipalloc failed")
    assert result.unread is True


def test_fetch_device_list_fallback_session_auth_error_propagates():
    """A lost router session during the ipalloc fallback is a no-answer, not an empty device list."""

    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise RouterAuthError("session expired")

    with pytest.raises(RouterAuthError, match="session expired"):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)


def test_fetch_device_list_fallback_page_level_auth_error_is_soft_failure():
    """ipalloc alone answering 401/403 is that page's failure; the session is not known to be lost."""

    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        error = RouterAuthError("ipalloc answered 403")
        error.status_code = 403
        error.page_level = True
        raise error

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert result == DeviceListResult(
        fallback=True, devices=[], error="boom", fallback_error="ipalloc answered 403"
    )
    assert result.unread is True


def test_fetch_device_list_fallback_read_with_zero_devices_is_an_answer():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        empty = ParsedPage(page="ipalloc", title="IP Allocation", heading="IP Allocation", tables=[])
        return SimpleNamespace(page=page, ok=True, status_code=200, parsed=empty, error=None)

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert result == DeviceListResult(fallback=True, devices=[], error="boom")
    assert result.unread is False


def test_fetch_device_list_fallback_other_error_propagates():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)


def test_fetch_device_list_pool_full_is_not_retried_against_ipalloc():
    """TS devices.ts:20 rethrows RouterAuthError, which includes RouterSessionPoolFullError."""
    fallback_calls: list[str] = []

    def get(page):
        raise session_pool_full_error(waited_ms=5, retry_count=1)

    def fetch(client, page, include_secrets=False):
        fallback_calls.append(page)
        raise AssertionError("ipalloc fallback must not run while the pool is full")

    with pytest.raises(RouterSessionPoolFullError):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert fallback_calls == []


def test_fetch_device_list_fallback_pool_full_propagates_so_the_cooldown_is_recorded():
    """A full session pool during the ipalloc fallback is not a soft per-page failure: swallowing it
    would return an empty device list with exit 0 and never write the cooldown (external review)."""

    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise session_pool_full_error(waited_ms=3, retry_count=1)

    with pytest.raises(RouterSessionPoolFullError):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)


def test_fetch_device_list_fallback_login_bounce_propagates():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise RouterAuthError("login page")

    with pytest.raises(RouterAuthError, match="login page"):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)


def test_fetch_device_list_page_level_auth_error_on_devices_falls_back_to_ipalloc():
    """devices.ha alone answering 401/403 is that page's failure: the ipalloc fallback still runs."""

    def get(page):
        error = RouterAuthError("Router rejected https://router/cgi-bin/devices.ha with HTTP 403.")
        error.status_code = 403
        error.page_level = True
        raise error

    ipalloc = ParsedPage(
        page="ipalloc", title="IP Allocation", heading="IP Allocation",
        tables=[{"IPv4 Address / Name": "192.168.1.20 / laptop", "MAC Address": "aa:bb", "Status": "on"}],
    )
    seen: list[str] = []

    def fetch(client, page, include_secrets=False):
        seen.append(page)
        return SimpleNamespace(page=page, ok=True, status_code=200, parsed=ipalloc, error=None)

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert seen == ["ipalloc"]
    assert result.fallback is True and [d.name for d in result.devices] == ["laptop"]
    assert "HTTP 403" in result.error


def test_fetch_device_list_session_wide_auth_error_with_status_still_raises():
    """A 403 from the login handshake carries a status but is not page-level: the session is gone."""

    def get(page):
        error = RouterAuthError("Router rejected https://router/cgi-bin/login.ha with HTTP 403.")
        error.status_code = 403
        raise error

    def fetch(client, page, include_secrets=False):
        raise AssertionError("ipalloc must not be read after a session-wide auth failure")

    with pytest.raises(RouterAuthError, match="login.ha"):
        fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)


# --- unavailable devices page and hostname-only fallback rows, through the public CLI ---------------

_NOT_FOUND = "<html><head><title>Page not found</title></head><body><h1>Page not found.</h1></body></html>"


def _bind_cli(monkeypatch, handler):
    from test_client import FakeTransport, make_client

    from bgwcli import cli

    transport = FakeTransport(handler)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    return cli, transport


def test_devices_page_not_found_falls_back_to_ipalloc(tmp_env, monkeypatch, capsys):
    import json

    from test_client import html
    from test_parser import IPALLOC_HTML

    def handler(request, _n):
        return html(_NOT_FOUND if "/devices.ha" in request.url else IPALLOC_HTML)

    cli, transport = _bind_cli(monkeypatch, handler)
    assert cli.main(["devices", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fallback"] is True and "Page not found" in payload["error"]
    assert [d["mac"].lower()[-2:] for d in payload["devices"]] == ["02", "04", "03"]
    assert [r.method for r in transport.requests] == ["GET", "GET"]


def test_devices_and_ipalloc_both_not_found_is_no_answer(tmp_env, monkeypatch, capsys):
    import json

    from test_client import html

    cli, transport = _bind_cli(monkeypatch, lambda _r, _n: html(_NOT_FOUND))
    assert cli.main(["devices", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["devices"] == [] and payload["fallbackError"]
    assert [r.method for r in transport.requests] == ["GET", "GET"]


def test_hostname_only_fallback_row_is_a_name_not_an_ip(tmp_env, monkeypatch, capsys):
    import json

    from test_client import html
    from test_parser import IPALLOC_HTML

    def handler(request, _n):
        if "/devices.ha" in request.url:
            raise RouterConnectionError("synthetic devices timeout")
        return html(IPALLOC_HTML)

    cli, transport = _bind_cli(monkeypatch, handler)
    assert cli.main(["devices", "--all", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    watch = next(d for d in payload["devices"] if d["mac"].endswith(":04"))
    assert (watch["ip"], watch["name"]) == ("", "watch")
    first = next(d for d in payload["devices"] if d["mac"].lower().endswith(":02"))
    assert (first["ip"], first["name"]) == ("192.168.1.64", "")
    assert len(transport.requests) == 2


def test_split_ip_and_name_classifies_a_slashless_value_as_address_or_name():
    assert split_ip_and_name("watch") == ("", "watch")
    assert split_ip_and_name("10.0.0.5") == ("10.0.0.5", "")
    assert split_ip_and_name("fe80::1") == ("fe80::1", "")
    assert split_ip_and_name("999.1.1.1") == ("", "999.1.1.1")


def test_a_200_body_without_the_device_table_is_unavailable_not_an_empty_list():
    ipalloc = _Response(
        "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>MAC Address</th><th>Allocation</th></tr>"
        "<tr><td>on</td><td>192.168.1.5 / laptop</td><td>00:11:22:33:44:55</td><td>dhcp</td></tr></table>"
    )

    def get(page):
        return _Response("<html><title>Please wait</title><p>loading</p></html>") if page == "devices" else ipalloc

    client = _Client(get)
    result = fetch_device_list(client, include_offline=True, parse_devices=lambda html: [])
    assert result.fallback is True and "no device table" in (result.error or "")
    assert client.calls == ["devices", "ipalloc"]
    assert fetch_device_list(_Client(lambda page: _Response(_DEVICE_TABLE)), parse_devices=lambda h: []).devices == []


def test_wide_device_rows_split_name_and_address_like_the_fallback_rows():
    from bgwcli.parser import parse_devices

    table = (
        "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>MAC Address</th><th>Connection</th></tr>"
        "<tr><td>on</td><td>192.168.1.5 / iPad/Anna</td><td>00:11:22:33:44:55</td><td>Wi-Fi</td></tr>"
        "<tr><td>on</td><td>192.168.1.6 /</td><td>00:11:22:33:44:56</td><td>Ethernet</td></tr></table>"
    )
    assert [(d.ip, d.name) for d in parse_devices(table)] == [("192.168.1.5", "iPad/Anna"), ("192.168.1.6", "")]


_IPALLOC_HEADER = (
    "<tr><th>Status</th><th>IPv4 Address / Name</th><th>MAC Address</th><th>Allocation</th></tr>"
)


def _hanging_devices(ipalloc_body: str):
    def get(page):
        if page == "devices":
            raise RouterConnectionError("devices.ha hangs")
        return _Response(ipalloc_body)

    return _Client(get)


@pytest.mark.parametrize(
    "body",
    [
        "<html><head><title>Please wait</title></head><body><p>Please wait...</p></body></html>",
        "<html><body><h1>IP Allocation</h1><p>nothing here</p></body></html>",
        "<html><body><table><tr><th>Name</th><th>Value</th></tr></table></body></html>",
    ],
)
def test_ipalloc_fallback_page_without_the_allocation_header_is_unread(body):
    result = fetch_device_list(_hanging_devices(body), include_offline=True, parse_devices=lambda html: [])
    assert result.fallback is True and result.devices == []
    assert result.unread is True and "IP Allocation" in (result.fallback_error or "")


def test_ipalloc_fallback_header_only_table_is_a_real_empty_answer():
    body = f"<html><body><h1>IP Allocation</h1><table>{_IPALLOC_HEADER}</table></body></html>"
    result = fetch_device_list(_hanging_devices(body), include_offline=True, parse_devices=lambda html: [])
    assert result.fallback is True and result.devices == []
    assert result.unread is False


def test_ipalloc_fallback_table_with_rows_is_read():
    body = (
        f"<html><body><table>{_IPALLOC_HEADER}"
        "<tr><td>on</td><td>192.168.1.5 / laptop</td><td>00:11:22:33:44:55</td><td>DHCP</td></tr>"
        "</table></body></html>"
    )
    result = fetch_device_list(_hanging_devices(body), include_offline=True, parse_devices=lambda html: [])
    assert result.unread is False
    assert [d.name for d in result.devices] == ["laptop"]
