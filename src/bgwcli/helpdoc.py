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


# ---------------------------------------------------------------------------------------------
# command help: section page blocks, positional text, option visibility (display only)

_DIAGNOSTIC_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("ping <host>", "Ping a host from the gateway"),
    ("traceroute <host>", "Trace the route to a host from the gateway"),
    ("nslookup <host>", "Resolve a name with the gateway's DNS"),
)


def page_rows_text(rows: list[PageRow], indent: str = "  ") -> str:
    """One row per page: slug, summary, then [default] / [guarded]. Rows never wrap."""
    width = max(len(row.slug) for row in rows) + 2
    lines = []
    for row in rows:
        markers = (" [default]" if row.is_default else "") + (" [guarded]" if row.guarded else "")
        lines.append(f"{indent}{row.slug.ljust(width)}{row.summary}{markers}")
    return "\n".join(lines)


def diagnostic_operations_text(indent: str = "  ") -> str:
    width = max(len(name) for name, _ in _DIAGNOSTIC_OPERATIONS) + 2
    return "\n".join(f"{indent}{name.ljust(width)}{summary}" for name, summary in _DIAGNOSTIC_OPERATIONS)


def section_help_epilog(section: RouterSectionName) -> str:
    rows = page_rows(section)
    command = section_command(section)
    parts = [f"Pages (bgwcli {command} <page>):", page_rows_text(rows)]
    if section == "Diagnostics":
        parts += [
            "",
            f"Operations (bgwcli {command} <operation> <host>; dry-run, add --commit --confirm DIAG to send):",
            diagnostic_operations_text(),
        ]
    example = next(row for row in rows if not row.is_default and not row.guarded)
    parts += [
        "",
        f'Labels and page ids work too, e.g. bgwcli {command} "{example.label}" or bgwcli {command} {example.page}.',
    ]
    return "\n".join(parts)


def section_description(section: RouterSectionName) -> str:
    command = section_command(section)
    if section == "Diagnostics":
        return (
            f"{command} [page] | {command} ping|traceroute|nslookup <host>: show one page of the "
            "Diagnostics section, or prepare a diagnostic test."
        )
    return f"{command} [page]: show one page of the {section} section."


def _section_arg(section: RouterSectionName) -> tuple[str, str]:
    default = next(row.slug for row in page_rows(section) if row.is_default)
    if section == "Diagnostics":
        return ("page|operation", f"page to show (default {default}) or ping|traceroute|nslookup <host>; listed below")
    return ("page", f"page to show (default {default}); pages are listed below")


_NO_ARGS = ("args", "uses no positional arguments")

COMMAND_ARGS: dict[str, tuple[str, str]] = {
    **{section_command(section): _section_arg(section) for section in (
        "Device", "Broadband", "Home Network", "Voice", "Firewall", "Diagnostics")},
    "section": (
        "section",
        "one section: device, broadband, home-network, voice, firewall, diagnostics "
        '(one word; a quoted "Home Network" also works)',
    ),
    "page": ("page", "page name, label, Section/Tab path or CGI page id (words are joined); bgwcli tabs lists them"),
    "inspect": ("page", "page name, label, Section/Tab path or CGI page id (words are joined); bgwcli tabs lists them"),
    "action": ("name", "action name or alias (e.g. reboot); bgwcli actions lists every action and its token"),
    "session": ("subcommand", "status (default) or clear-cache"),
    "set": ("page KEY=VALUE", "form page, then one or more field assignments"),
    "submit": ("page button [KEY=VALUE]", "form page, button name, then optional field assignments"),
    "diff": ("dumpfile", "file written by bgwcli dump"),
    "restore": ("dumpfile", "file written by bgwcli dump"),
    "autorestore": ("dumpfile", "file written by bgwcli dump"),
    "help": ("command", "show help for one command, e.g. bgwcli help device"),
    **{name: _NO_ARGS for name in (
        "check", "auth", "sitemap", "coverage", "tabs", "actions", "sweep", "scan", "schema", "audit",
        "readiness", "devices", "wifi", "nat", "logs", "status", "dump", "fixtures-capture")},
}

_PAGE_VIEW = frozenset({"--raw", "--forms", "--limit"})
_SWEEP = frozenset({"--delay", "--pages", "--include-parsed", "--full", "--forms", "--raw", "--out"})
_COMMIT = frozenset({"--commit", "--confirm"})

# Command options each command's help shows. Hidden options are still parsed exactly as before.
COMMAND_OPTION_USE: dict[str, frozenset[str]] = {
    "device": _PAGE_VIEW | {"--all"},
    "broadband": _PAGE_VIEW,
    "home-network": _PAGE_VIEW,
    "voice": _PAGE_VIEW,
    "firewall": _PAGE_VIEW,
    "diagnostics": _PAGE_VIEW | _COMMIT | {"--ipv4", "--ipv6"},
    "page": _PAGE_VIEW | {"--all"},
    "inspect": frozenset({"--raw", "--limit", "--all"}),
    "wifi": _PAGE_VIEW,
    "nat": _PAGE_VIEW,
    "logs": frozenset({"--raw", "--limit"}),
    "devices": frozenset({"--raw", "--limit", "--all"}),
    "status": frozenset(),
    "sweep": _SWEEP,
    "scan": _SWEEP,
    "schema": frozenset({"--delay", "--pages", "--raw", "--out"}),
    "audit": frozenset({"--delay", "--pages"}),
    "readiness": frozenset({"--delay", "--pages"}),
    "action": _COMMIT,
    "set": _COMMIT,
    "submit": _COMMIT,
    "dump": frozenset({"--out", "--include", "--all-clients"}),
    "diff": frozenset({"--include"}),
    "restore": frozenset({"--prune", "--include"}) | _COMMIT,
    "autorestore": frozenset({"--include", "--max-passes", "--wait", "--on-any-diff"}) | _COMMIT,
    "fixtures-capture": frozenset({"--out", "--delay", "--pages"}),
    **{name: frozenset() for name in ("check", "auth", "sitemap", "coverage", "tabs", "actions", "section",
                                     "session", "help")},
}

_ALL_GLOBALS = frozenset({
    "--host", "--config", "--access-code-stdin", "--json", "--include-secrets", "--timeout", "--strict-tls",
    "--wait-for-session", "--session-wait-timeout", "--session-wait-interval",
})
_NO_SECRETS = _ALL_GLOBALS - {"--include-secrets"}

# Global options each command's help shows: the ones its handler path reads.
GLOBAL_OPTION_USE: dict[str, frozenset[str]] = {
    **{name: _ALL_GLOBALS for name in COMMAND_OPTION_USE},
    "check": _NO_SECRETS,
    "auth": _NO_SECRETS,  # --include-secrets has no effect on a successful auth result
    "fixtures-capture": _NO_SECRETS,  # forces include_secrets off
    "sitemap": frozenset({"--host", "--config", "--json", "--timeout", "--strict-tls"}),  # public GET, no login
    "coverage": frozenset({"--host", "--config", "--json", "--timeout", "--strict-tls"}),
    "session": frozenset({"--host", "--config", "--json"}),  # --host selects the cache inspected/cleared
    "tabs": frozenset({"--json"}),
    "actions": frozenset({"--json"}),
    "section": frozenset({"--json"}),
    "help": frozenset(),
}

OPTION_METAVAR_OVERRIDES: dict[str, dict[str, str]] = {"dump": {"--out": "<file>"}}


# ---------------------------------------------------------------------------------------------
# root help

_ROOT_SECTION_NOTES: dict[RouterSectionName, tuple[str, ...]] = {
    "Device": (
        "Note: status is a concise overview; if home.ha hangs it falls back to System Information,",
        "      Broadband Status and Firewall Status without touching flaky LAN/Fiber pages.",
    ),
    "Firewall": (
        "Note: security-options falls back to Firewall Status plus Firewall Advanced on firmware",
        "      where securityoptions.ha returns Page not found.",
    ),
}


def root_pages_block() -> str:
    """Every page of every section with its summary, for ``bgwcli -h``."""
    parts = ["Router pages (bgwcli <section> <page>; details: bgwcli <section> -h):"]
    sections = list(dict.fromkeys(row.section for row in page_rows()))
    for section in sections:
        command = section_command(section)
        parts.append(f"  bgwcli {command}")
        parts.append(page_rows_text(page_rows(section), indent="    "))
        if section == "Diagnostics":
            parts.append(diagnostic_operations_text(indent="    "))
        parts.extend(f"    {note}" for note in _ROOT_SECTION_NOTES.get(section, ()))
    return "\n".join(parts)
