"""Port of tests/pages.test.ts."""

from bgwcli.pages import (
    ROUTER_TABS,
    RouterTab,
    list_sections,
    mapped_pages,
    resolve_page,
    resolve_section_command,
    tabs_for_section,
)
from bgwcli.types import to_json_dict


def test_router_tabs_covers_requested_sections():
    assert list_sections() == ["Device", "Broadband", "Home Network", "Voice", "Firewall", "Diagnostics"]
    assert len(ROUTER_TABS) == 37
    assert all(isinstance(tab, RouterTab) for tab in ROUTER_TABS)


def test_router_tabs_includes_every_tab_from_the_stated_objective():
    required = [
        "Device/Status", "Device/Device List", "Device/System Information", "Device/Access Code",
        "Device/Remote Access", "Device/Restart Device",
        "Broadband/Status", "Broadband/Configure", "Broadband/Fiber Status",
        "Home Network/Status", "Home Network/Configure", "Home Network/IPv6", "Home Network/Wi-Fi",
        "Home Network/Advanced Wi-Fi", "Home Network/MAC Filtering", "Home Network/Subnets & DHCP",
        "Home Network/IP Allocation",
        "Voice/Status", "Voice/Line Details", "Voice/Call Statistics",
        "Firewall/Status", "Firewall/Packet Filter", "Firewall/NAT/Gaming", "Firewall/Public Subnet Hosts",
        "Firewall/IP Passthrough", "Firewall/Firewall Advanced", "Firewall/Security Options",
        "Diagnostics/Troubleshoot", "Diagnostics/Speed Test", "Diagnostics/Logs", "Diagnostics/Update",
        "Diagnostics/Resets", "Diagnostics/Syslog", "Diagnostics/Event Notifications", "Diagnostics/NAT Table",
    ]
    present = {f"{tab.section}/{tab.label}" for tab in ROUTER_TABS}
    assert set(required) <= present


def test_resolve_page_accepts_raw_cgi_ids_and_human_tab_paths():
    assert resolve_page("wconfig_unified") == "wconfig_unified"
    assert resolve_page("Home Network/Wi-Fi") == "wconfig_unified"
    assert resolve_page("Home Network/Advanced Wi-Fi") == "wconfig"
    assert resolve_page("Subnets & DHCP") == "dhcpserver"
    assert resolve_page("NAT/Gaming") == "apphosting"
    assert resolve_page("Device/Restart Device") == "restart"


def test_resolve_page_passes_unknown_input_through():
    assert resolve_page("not-a-page") == "not-a-page"


def test_tabs_for_section_returns_section_entries():
    assert [tab.page for tab in tabs_for_section("diagnostics")] == [
        "diag", "speed", "logs", "update", "reset", "syslog", "events", "nattable", "sitemap",
    ]
    assert tabs_for_section("nope") == []


def test_resolve_section_command_resolves_human_command_tree():
    assert resolve_section_command("device", []).page == "home"
    assert resolve_section_command("broadband", ["fiber-status"]).page == "fiberstat"
    assert resolve_section_command("home-network", ["wi-fi"]).page == "wconfig_unified"
    assert resolve_section_command("home-network", ["advanced-wi-fi"]).page == "wconfig"
    assert resolve_section_command("firewall", ["nat-gaming"]).page == "apphosting"
    assert resolve_section_command("diagnostics", ["nat-table"]).page == "nattable"


def test_resolve_section_command_defaults_and_misses():
    # Diagnostics defaults to Troubleshoot; every other section defaults to Status.
    assert resolve_section_command("diag", []).page == "diag"
    assert resolve_section_command("lan", []).page == "lanstatistics"
    assert resolve_section_command("voice", ["line", "details"]).page == "voiceconfig"
    assert resolve_section_command("unknown-root", []) is None
    assert resolve_section_command("device", ["no-such-tab"]) is None


def test_dangerous_flags_and_mapped_pages():
    dangerous = {tab.page for tab in ROUTER_TABS if tab.dangerous}
    assert dangerous == {"routerpasswd", "restart", "update", "reset"}
    pages = mapped_pages()
    assert pages == sorted(set(pages))
    assert "sitemap" in pages and "home" in pages


def test_router_tab_json_omits_dangerous_unless_true():
    """TS `routerTabs` only carry `dangerous: true` on the four guarded tabs; the key is absent elsewhere."""
    payload = to_json_dict(list(ROUTER_TABS))
    by_page = {tab["page"]: tab for tab in payload}
    assert "dangerous" not in by_page["sysinfo"]
    assert by_page["restart"]["dangerous"] is True
    assert {p for p, t in by_page.items() if "dangerous" in t} == {"routerpasswd", "restart", "update", "reset"}
    # truthiness semantics preserved for code that reads tab.dangerous / tab.is_dangerous
    sysinfo = next(tab for tab in ROUTER_TABS if tab.page == "sysinfo")
    restart = next(tab for tab in ROUTER_TABS if tab.page == "restart")
    assert not sysinfo.dangerous and sysinfo.is_dangerous is False
    assert restart.dangerous and restart.is_dangerous is True
