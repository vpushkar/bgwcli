"""Help text derived from the router tab map: page names, summaries and rendered help blocks."""

from __future__ import annotations

import pytest

from bgwcli import cli, helpdoc
from bgwcli.pages import ROUTER_TABS, list_sections, resolve_section_command

ELLIPSES = ("...", "…")


def _tab(section: str, label: str):
    return next(tab for tab in ROUTER_TABS if tab.section == section and tab.label == label)


@pytest.mark.parametrize(
    ("section", "label", "slug"),
    [
        ("Device", "System Information", "system-information"),
        ("Home Network", "Subnets & DHCP", "subnets-dhcp"),
        ("Firewall", "NAT/Gaming", "nat-gaming"),
        ("Home Network", "Wi-Fi", "wi-fi"),
        ("Home Network", "Advanced Wi-Fi", "advanced-wi-fi"),
        ("Home Network", "IPv6", "ipv6"),
        ("Diagnostics", "Site Map", "site-map"),
    ],
)
def test_slug_examples(section, label, slug):
    assert helpdoc.tab_slug(_tab(section, label)) == slug


def test_every_slug_resolves_to_its_own_tab():
    # subnets-dhcp resolves only through the explicit "subnets dhcp" alias: removing it must fail here.
    for tab in ROUTER_TABS:
        command = helpdoc.section_command(tab.section)
        roots = (command, *cli.SECTION_COMMANDS[command])
        for root in roots:
            assert resolve_section_command(root, [helpdoc.tab_slug(tab)]) is tab, (root, tab.label)


def test_slugs_unique_within_section():
    for section in list_sections():
        slugs = [helpdoc.tab_slug(tab) for tab in ROUTER_TABS if tab.section == section]
        assert len(slugs) == len(set(slugs)), section


def test_section_commands_match_cli():
    assert {helpdoc.section_command(section) for section in list_sections()} == set(cli.SECTION_COMMANDS)


def test_every_tab_has_a_summary():
    assert set(helpdoc.TAB_SUMMARIES) == {tab.page for tab in ROUTER_TABS}
    for page, summary in helpdoc.TAB_SUMMARIES.items():
        assert summary.strip(), page
        assert len(summary) <= 60, page
        assert not summary.endswith("."), page
        assert not any(mark in summary for mark in ELLIPSES), page


def test_sysinfo_summary_names_the_firmware_field():
    assert "Software Version" in helpdoc.TAB_SUMMARIES["sysinfo"]


def test_page_rows_cover_every_tab_in_map_order():
    rows = helpdoc.page_rows()
    assert [row.page for row in rows] == [tab.page for tab in ROUTER_TABS]
    device = helpdoc.page_rows("Device")
    assert [row.slug for row in device][:3] == ["status", "device-list", "system-information"]
    assert device[0].is_default and not device[1].is_default
    assert {row.page for row in rows if row.guarded} == {"routerpasswd", "restart", "update", "reset"}
    assert all(row.command == helpdoc.section_command(row.section) for row in rows)
    troubleshoot = next(row for row in rows if row.page == "diag")
    assert troubleshoot.is_default
