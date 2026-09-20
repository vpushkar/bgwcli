"""Router navigation map: sections, tabs, CGI page ids and the aliases users may type.

Port of src/pages.ts. Every mapping is preserved verbatim; matching is done on a normalized key
(lower-case, ``&`` -> ``and``, everything non-alphanumeric dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RouterSectionName = Literal["Device", "Broadband", "Home Network", "Voice", "Firewall", "Diagnostics"]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RouterTab:
    section: RouterSectionName
    label: str
    page: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # True on guarded tabs, None otherwise: TS `routerTabs` only carry `dangerous: true`, and
    # to_json_dict() drops None, so `tabs --json` omits the key exactly like JSON.stringify does.
    dangerous: bool | None = None

    @property
    def is_dangerous(self) -> bool:
        return bool(self.dangerous)

    @property
    def path(self) -> str:
        return f"{self.section}/{self.label}"

    def matches(self, normalized: str) -> bool:
        candidates = (self.page, self.label, self.path, f"{self.section} {self.label}", *self.aliases)
        return any(normalize_key(candidate) == normalized for candidate in candidates)


def _tab(
    section: RouterSectionName, label: str, page: str, aliases: tuple[str, ...], *, dangerous: bool = False
) -> RouterTab:
    return RouterTab(section=section, label=label, page=page, aliases=aliases, dangerous=True if dangerous else None)


ROUTER_TABS: tuple[RouterTab, ...] = (
    _tab("Device", "Status", "home", ("device", "device status", "status")),
    _tab("Device", "Device List", "devices", ("device list", "devices")),
    _tab("Device", "System Information", "sysinfo", ("system information", "sysinfo", "system info")),
    _tab("Device", "Access Code", "routerpasswd", ("access code", "router password", "routerpasswd"), dangerous=True),
    _tab("Device", "Remote Access", "remoteaccess", ("remote access", "remoteaccess")),
    _tab("Device", "Restart Device", "restart", ("restart device", "restart"), dangerous=True),
    _tab("Broadband", "Status", "broadbandstatistics", ("broadband", "broadband status", "broadbandstatistics")),
    _tab("Broadband", "Configure", "broadbandconfig", ("broadband configure", "broadband config", "broadbandconfig")),
    _tab("Broadband", "Fiber Status", "fiberstat", ("fiber", "fiber status", "fiberstat")),
    _tab(
        "Home Network", "Status", "lanstatistics",
        ("home network", "home network status", "lanstatistics", "lan status"),
    ),
    _tab("Home Network", "Configure", "etherlan", ("home network configure", "etherlan", "ethernet lan")),
    _tab("Home Network", "IPv6", "ip6lan", ("ipv6", "ip6lan")),
    _tab("Home Network", "Wi-Fi", "wconfig_unified", ("wifi", "wi-fi", "wireless", "wconfig_unified")),
    _tab("Home Network", "Advanced Wi-Fi", "wconfig", ("advanced wifi", "advanced wi-fi", "wifi advanced", "wconfig")),
    _tab("Home Network", "MAC Filtering", "wmacauth", ("mac filtering", "wifi mac filtering", "wmacauth")),
    _tab(
        "Home Network", "Subnets & DHCP", "dhcpserver",
        ("subnets", "dhcp", "subnets dhcp", "subnets and dhcp", "dhcpserver"),
    ),
    _tab("Home Network", "IP Allocation", "ipalloc", ("ip allocation", "ipalloc")),
    _tab("Voice", "Status", "voice", ("voice", "voice status")),
    _tab("Voice", "Line Details", "voiceconfig", ("line details", "voice line details", "voiceconfig")),
    _tab("Voice", "Call Statistics", "voicestat", ("call statistics", "voice call statistics", "voicestat")),
    _tab("Firewall", "Status", "firewall", ("firewall", "firewall status")),
    _tab("Firewall", "Custom Services", "services", ("custom services", "services")),
    _tab("Firewall", "Packet Filter", "packetfilter", ("packet filter", "packetfilter")),
    _tab("Firewall", "NAT/Gaming", "apphosting", ("nat gaming", "nat/gaming", "gaming", "apphosting")),
    _tab("Firewall", "Public Subnet Hosts", "pshosts", ("public subnet hosts", "pshosts")),
    _tab("Firewall", "IP Passthrough", "ippass", ("ip passthrough", "ippass")),
    _tab("Firewall", "Firewall Advanced", "dosprotect", ("firewall advanced", "advanced firewall", "dosprotect")),
    _tab("Firewall", "Security Options", "securityoptions", ("security options", "securityoptions")),
    _tab("Diagnostics", "Troubleshoot", "diag", ("troubleshoot", "diagnostics", "diag")),
    _tab("Diagnostics", "Speed Test", "speed", ("speed test", "speed")),
    _tab("Diagnostics", "Logs", "logs", ("logs",)),
    _tab("Diagnostics", "Update", "update", ("update", "firmware update"), dangerous=True),
    _tab("Diagnostics", "Resets", "reset", ("resets", "reset"), dangerous=True),
    _tab("Diagnostics", "Syslog", "syslog", ("syslog",)),
    _tab("Diagnostics", "Event Notifications", "events", ("event notifications", "events")),
    _tab("Diagnostics", "NAT Table", "nattable", ("nat table", "nattable")),
    _tab("Diagnostics", "Site Map", "sitemap", ("site map", "sitemap")),
)

# TS-compatible spelling for callers porting ``routerTabs`` imports.
router_tabs = ROUTER_TABS

_SECTION_ROOTS: dict[str, RouterSectionName] = {
    "device": "Device",
    "broadband": "Broadband",
    "homenetwork": "Home Network",
    "home": "Home Network",
    "lan": "Home Network",
    "voice": "Voice",
    "firewall": "Firewall",
    "diagnostics": "Diagnostics",
    "diagnostic": "Diagnostics",
    "diag": "Diagnostics",
}


def normalize_key(value: str) -> str:
    """Lower-case, ``&`` -> ``and``, drop every non-alphanumeric character."""
    return _NON_ALNUM.sub("", value.lower().replace("&", "and"))


def list_sections() -> list[RouterSectionName]:
    return list(dict.fromkeys(tab.section for tab in ROUTER_TABS))


def tabs_for_section(section: str) -> list[RouterTab]:
    key = normalize_key(section)
    return [tab for tab in ROUTER_TABS if normalize_key(tab.section) == key]


def resolve_tab(value: str) -> RouterTab | None:
    normalized = normalize_key(value)
    return next((tab for tab in ROUTER_TABS if tab.matches(normalized)), None)


def resolve_page(value: str) -> str:
    """Map a CGI id, tab label, ``Section/Tab`` path or alias to its CGI page id; unknown input passes through."""
    tab = resolve_tab(value)
    return tab.page if tab else value


def section_for_root(root: str) -> RouterSectionName | None:
    return _SECTION_ROOTS.get(normalize_key(root))


def default_tab_for_section(section: RouterSectionName) -> str:
    return "Troubleshoot" if section == "Diagnostics" else "Status"


def resolve_section_command(root: str, args: list[str]) -> RouterTab | None:
    """Resolve ``bgwcli <section> [tab words...]`` to a tab; None when the root or tab is unknown."""
    section = section_for_root(root)
    if section is None:
        return None
    tab_input = " ".join(args) if args else default_tab_for_section(section)
    normalized = normalize_key(tab_input)
    return next((tab for tab in ROUTER_TABS if tab.section == section and tab.matches(normalized)), None)


def mapped_pages() -> list[str]:
    """Sorted unique CGI page ids covered by the tab map (the router fixture pack's page list)."""
    return sorted({tab.page for tab in ROUTER_TABS})


def dangerous_pages() -> frozenset[str]:
    return frozenset(tab.page for tab in ROUTER_TABS if tab.dangerous)
