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
    assert split_ip_and_name("192.168.1.2/host") == ("192.168.1.2/host", "")
    assert split_ip_and_name("") == ("", "")
    assert split_ip_and_name("1.2.3.4 / a / b") == ("1.2.3.4", "a / b")


def _dev(status: str, mac: str = "00:00:00:00:00:01") -> Device:
    return Device(status=status, name="n", ip="", mac=mac, connection="")


def test_filter_devices_keeps_only_online_unless_include_offline():
    devices = [_dev("on"), _dev("off"), _dev("Online"), _dev("")]
    assert [d.status for d in filter_devices(devices, include_offline=False)] == ["on", "Online"]
    assert filter_devices(devices, include_offline=True) == devices


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
    client = _Client(lambda page: _Response("<html/>"))
    parsed = [_dev("on"), _dev("off")]
    result = fetch_device_list(client, include_offline=False, parse_devices=lambda html: parsed)
    assert result == DeviceListResult(fallback=False, devices=[parsed[0]])
    assert client.calls == ["devices"]
    assert to_json_dict(result) == {
        "fallback": False,
        "devices": [{"status": "on", "name": "n", "ip": "", "mac": "00:00:00:00:00:01", "connection": ""}],
    }


def test_fetch_device_list_include_offline_keeps_all():
    client = _Client(lambda page: _Response("<html/>"))
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
    assert result == DeviceListResult(fallback=True, devices=[], error="boom")


def test_fetch_device_list_fallback_auth_error_is_soft_failure():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise RouterAuthError("session expired")

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert result == DeviceListResult(fallback=True, devices=[], error="boom")


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


def test_fetch_device_list_fallback_plain_auth_error_is_still_soft():
    def get(page):
        raise RouterConnectionError("boom")

    def fetch(client, page, include_secrets=False):
        raise RouterAuthError("login page")

    result = fetch_device_list(_Client(get), parse_devices=lambda html: [], fetch_parsed_page=fetch)
    assert result == DeviceListResult(fallback=True, devices=[], error="boom")
