"""Parser checks over a small committed set of synthetic, real-shaped router pages.

The captured router fixture pack is gitignored, so its tests skip on a fresh checkout; these pages
(tests/fixtures-synthetic/) follow the live firmware's markup conventions (quoted attributes,
key fields as text inputs, nonce in every form) and always run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bgwcli.devices import devices_from_ip_allocation_rows, filter_devices
from bgwcli.fixture_sanitize import contains_sensitive_fixture_value, sanitize_router_fixture, sensitive_control_residue
from bgwcli.parser import looks_like_login, parse_page

SYNTHETIC = Path(__file__).parent / "fixtures-synthetic"
PAGES = sorted(path.stem for path in SYNTHETIC.glob("*.html"))


def html(page: str) -> str:
    return (SYNTHETIC / f"{page}.html").read_text(encoding="utf-8")


def test_synthetic_set_is_present():
    assert PAGES == ["ipalloc", "login", "routerpasswd", "sysinfo", "wconfig_unified"]


def test_sysinfo_key_value_table():
    parsed = parse_page("sysinfo", html("sysinfo"))
    assert parsed.title == "System Information"
    assert parsed.values["Model Number"] == "BGW320-505" and parsed.values["Software Version"] == "6.35.8"
    assert parsed.value_entries[0].section == "System Information" and parsed.tables == []


def test_wifi_form_redacts_the_key_and_reads_radio_channels():
    parsed = parse_page("wconfig_unified", html("wconfig_unified"))
    fields = {f.name: f.value for f in parsed.fields}
    assert fields == {"nonce": "[redacted]", "home_ssidname": "SyntheticHome", "homeSSID_key": "[redacted]"}
    assert "synthetic-passphrase" not in repr(parsed)
    assert parse_page("wconfig_unified", html("wconfig_unified"), include_secrets=True).values[
        "Field homeSSID_key"
    ] == "synthetic-passphrase"
    assert [(row["Radio"], row["Current Channel"]) for row in parsed.tables] == [
        ("2.4 GHz", "6"), ("5 GHz low-band", "36"), ("5 GHz high-band", "149"),
    ]
    assert [b.name for b in parsed.buttons] == ["Save"] and parsed.forms[0].action == "/cgi-bin/wconfig_unified.ha"


def test_ip_allocation_table_feeds_the_device_fallback():
    parsed = parse_page("ipalloc", html("ipalloc"))
    assert [row["Status"] for row in parsed.tables] == ["on", "off"]
    devices = filter_devices(devices_from_ip_allocation_rows(parsed.tables), include_offline=False)
    assert [(d.ip, d.name) for d in devices] == [("192.0.2.10", "laptop")]
    assert [b.name for b in parsed.buttons] == ["Allocate_1", "Release_2"]


@pytest.mark.parametrize("page", PAGES)
def test_only_the_login_page_looks_like_login(page):
    assert looks_like_login(html(page)) is (page == "login")


@pytest.mark.parametrize("page", PAGES)
def test_sanitized_synthetic_pages_carry_no_secret_residue(page):
    sanitized = sanitize_router_fixture(html(page))
    assert contains_sensitive_fixture_value(sanitized) is False
    assert sensitive_control_residue(parse_page(page, sanitized, include_secrets=True)) == []
    assert "02:00:00:00:00:01" not in sanitized and "synthetic-passphrase" not in sanitized
