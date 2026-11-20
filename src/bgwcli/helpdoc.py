"""Help text derived from the router tab map, so no page list in help or README can fall behind the code.

Page names ("slugs") are the words a user types after a section command (``bgwcli device
system-information``); every slug is checked by the tests to resolve back to its own tab.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .pages import ROUTER_TABS, RouterSectionName, RouterTab, default_tab_for_section

_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")

# One line per tab: what the page shows. Keyed by CGI page id.
TAB_SUMMARIES: dict[str, str] = {
    "home": "Gateway overview: broadband, Wi-Fi, LAN and uptime",
    "devices": "Devices on the network with address and connection",
    "sysinfo": "Model, serial numbers, firmware (Software Version)",
    "routerpasswd": "Device access code page",
    "remoteaccess": "Remote management access settings",
    "restart": "Gateway restart page",
    "broadbandstatistics": "Broadband link status, addresses and counters",
    "broadbandconfig": "Broadband connection settings",
    "fiberstat": "Optical module and fiber signal levels",
    "lanstatistics": "LAN and Wi-Fi traffic counters",
    "etherlan": "LAN Ethernet port settings",
    "ip6lan": "LAN IPv6 settings",
    "wconfig_unified": "Wi-Fi networks, names and passwords",
    "wconfig": "Advanced Wi-Fi radio settings and channels",
    "wmacauth": "Wi-Fi MAC address filtering",
    "dhcpserver": "LAN subnets and DHCP server settings",
    "ipalloc": "IP address allocation and fixed reservations",
    "voice": "Phone line status",
    "voiceconfig": "Provisioned phone line details",
    "voicestat": "Phone call statistics",
    "firewall": "Firewall status summary",
    "services": "Custom service (port) definitions",
    "packetfilter": "Packet filter rules",
    "apphosting": "NAT/Gaming port forwards",
    "pshosts": "Public subnet host settings",
    "ippass": "IP Passthrough settings",
    "dosprotect": "Advanced firewall protections",
    "securityoptions": "Firewall security options",
    "diag": "Troubleshooting tests and diagnostic details",
    "speed": "Speed test history",
    "logs": "Router event log",
    "update": "Firmware update page",
    "reset": "Reset and restart options",
    "syslog": "Remote syslog settings",
    "events": "Event notification settings",
    "nattable": "Active NAT sessions",
    "sitemap": "The router's own page index",
}


class PageRow(NamedTuple):
    section: RouterSectionName
    command: str
    slug: str
    page: str
    label: str
    summary: str
    is_default: bool
    guarded: bool


def section_command(section: RouterSectionName) -> str:
    """The CLI command for a router section: ``Home Network`` -> ``home-network``."""
    return section.lower().replace(" ", "-")


def tab_slug(tab: RouterTab) -> str:
    """The page name typed after the section command: ``Subnets & DHCP`` -> ``subnets-dhcp``."""
    return _NON_ALNUM_RUN.sub("-", tab.label.lower().replace("&", " ")).strip("-")


def page_rows(section: RouterSectionName | None = None) -> list[PageRow]:
    """Every tab (or one section's tabs) in map order, with its slug, summary and markers."""
    return [
        PageRow(
            section=tab.section,
            command=section_command(tab.section),
            slug=tab_slug(tab),
            page=tab.page,
            label=tab.label,
            summary=TAB_SUMMARIES[tab.page],
            is_default=tab.label == default_tab_for_section(tab.section),
            guarded=bool(tab.dangerous),
        )
        for tab in ROUTER_TABS
        if section is None or tab.section == section
    ]
