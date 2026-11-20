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
    "autorestore": _NO_SECRETS,  # forces redaction
    "dump": _NO_SECRETS,  # the dump file always keeps secrets; stdout is a summary
    "logs": _NO_SECRETS,  # the log view ignores the flag
    "devices": _NO_SECRETS,  # the device view ignores the flag
    "sitemap": frozenset({"--host", "--config", "--json", "--timeout", "--strict-tls"}),  # public GET, no login
    "coverage": frozenset({"--host", "--config", "--json", "--timeout", "--strict-tls"}),
    "session": frozenset({"--host", "--config", "--json"}),  # --host selects the cache inspected/cleared
    "tabs": frozenset({"--config", "--json"}),  # a config file can set BGW_JSON
    "actions": frozenset({"--config", "--json"}),  # a config file can set BGW_JSON
    "section": frozenset({"--config", "--json"}),  # a config file can set BGW_JSON
    "help": frozenset(),
}

OPTION_METAVAR_OVERRIDES: dict[str, dict[str, str]] = {"dump": {"--out": "<file>"}}

_PAGES_HELP = "Limit the traversal to these CGI page ids (comma-separated; ids are the third column of `bgwcli tabs`)"
_FORMS_HELP = "Include form controls in the JSON output (text output shows form counts only)"

# Per-command help text for shared options whose generic wording would mislead there (display only).
OPTION_HELP_OVERRIDES: dict[str, dict[str, str]] = {
    "sweep": {"--forms": _FORMS_HELP, "--pages": _PAGES_HELP},
    "scan": {"--forms": _FORMS_HELP, "--pages": _PAGES_HELP},
    "schema": {"--pages": _PAGES_HELP},
    "audit": {"--pages": _PAGES_HELP},
    "readiness": {"--pages": _PAGES_HELP},
    "fixtures-capture": {
        "--pages": _PAGES_HELP,
        "--out": "Fixture pack directory to write sanitized captures into. Default: ./tests/fixtures",
    },
}


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


# ---------------------------------------------------------------------------------------------
# backup, write and fetch commands: lists generated from the registries the code enforces


def _rows(pairs: list[tuple[str, str]], indent: str = "  ") -> str:
    width = max(len(name) for name, _ in pairs) + 2
    return "\n".join(f"{indent}{name.ljust(width)}{text}" for name, text in pairs)


# What each page `dump` reads by default adds to the dump file (snapshot.snapshot_pages_for(())).
DUMP_CONTRIBUTIONS: dict[str, str] = {
    "sysinfo": "firmware version only (dump metadata)",
    "services": "custom services",
    "apphosting": "NAT/Gaming forwards",
    "ipalloc": "fixed reservations (+ allocation table, Fixed rows unless --all-clients)",
    "packetfilter": "packet-filter table for reference; best effort, omitted if unreadable",
    "dosprotect": "Firewall Advanced form values",
    "wconfig": "Advanced Wi-Fi form values",
}

# Optional form pages (snapshot.OPTIONAL_FORM_PAGES): what they capture, and the risk of restoring them.
INCLUDE_RISK_NOTES: dict[str, str] = {
    "etherlan": "LAN port settings; restoring can cut the LAN port you are on",
    "dhcpserver": "Subnets & DHCP; restoring can move the gateway address (restored last)",
    "ippass": "IP Passthrough; restoring changes which LAN client gets the public address",
    "wmacauth": (
        "Wi-Fi MAC filtering modes; disabling filtering (mode none) needs no recorded list; enabling allow/deny on "
        "any network needs a nonempty recorded list with every entry on the gateway, and the check covers the "
        "whole page, so a save that enables filtering on one network can also hold up disabling it on another"
    ),
}

_EXIT_CODES: dict[str, tuple[tuple[str, str], ...]] = {
    "dump": (
        ("0", "dump file written"),
        ("1", "usage error (e.g. an unknown --include id)"),
        ("2", "the router or a page could not be read, or the snapshot could not be built"),
        ("2", "the --out destination is unusable or could not be written"),
        ("130", "interrupted (Ctrl-C)"),
    ),
    "diff": (
        ("0", "identical (a firmware-only change is still 0)"),
        ("1", "differences found, or none of the --include pages is in the dump"),
        ("2", "the dump file or the router could not be read"),
        ("130", "interrupted (Ctrl-C)"),
    ),
    "restore": (
        ("0", "dry run: plan printed (even when work remains)"),
        ("0", "--commit: the closing diff finds no selected dumped service, forward, reservation or form value "
              "still missing or different (--prune: and no extra service or forward); form fields the live page "
              "disables or does not render, controls only on the router, and extra reservations are ignored"),
        ("1", "not converged, a write rejected, confirmation refused, or nothing to compare"),
        ("2", "the dump file or the router could not be read"),
        ("2", "the allocation preflight is blocked (a fixed allocation held by another MAC, no enabled Clear and "
              "Rescan button, or a failed preflight or rescan), also in a dry run"),
        ("2", "a write was sent and never acknowledged (even when the closing diff is readable)"),
        ("130", "interrupted (Ctrl-C); the write state is reported"),
    ),
    "autorestore": (
        ("0", "no reset, converged, or router unreachable (also during the allocation preflight, before any write)"),
        ("1", "a restore is needed (dry run) or did not converge (the next timer run retries)"),
        ("2", "page-level 401/403 or an initial structural failure"),
        ("2", "the allocation preflight was blocked by a conflict or failed for another reason"),
        ("2", "a write was sent and never acknowledged, or the closing verification failed"),
        ("2", "a recovery checkpoint could not be read or written, another unfinished recovery is recorded, or the "
              "same failure ended three consecutive runs"),
        ("130", "interrupted (Ctrl-C)"),
    ),
}


def exit_codes_epilog(command: str) -> str:
    return "Exit codes:\n" + _rows(list(_EXIT_CODES[command]))


def dump_pages_epilog() -> str:
    from .snapshot import OPTIONAL_FORM_PAGES, snapshot_pages_for

    default = [(page, DUMP_CONTRIBUTIONS[page]) for page in snapshot_pages_for(())]
    optional = [(page, INCLUDE_RISK_NOTES[page]) for page in OPTIONAL_FORM_PAGES]
    return "\n".join([
        "Read by every dump:",
        _rows(default),
        "",
        "Optional (--include <id>[,<id>] or --include all):",
        _rows(optional),
        f"  all = {', '.join(OPTIONAL_FORM_PAGES)}; dosprotect and wconfig are accepted but always read;",
        "  any other id (e.g. services) is rejected. Capturing is read-only; the notes are restore risks.",
        "",
        "The dump file keeps Wi-Fi keys and other secrets in clear text (owner-only file), with or without",
        "--include-secrets.",
    ])


def include_ids_epilog(command: str) -> str:
    from .snapshot_diff import RESTORABLE_PAGES

    pairs = [(page, TAB_SUMMARIES[page]) for page in RESTORABLE_PAGES]
    lines = [
        "Pages --include accepts (--include <id>[,<id>] or all):",
        _rows(pairs),
        "  omitted or all = every restorable page present in the dump; a selection of pages that are",
        "  not in the dump compares nothing and exits 1.",
    ]
    if command == "autorestore":
        lines.append("  The selection also limits which pages count toward reset detection.")
    if command in ("restore", "autorestore"):
        lines.append("  Wi-Fi MAC filtering: restoring mode none (disable) needs no recorded list; allow/deny on any")
        lines.append("  network needs a nonempty recorded list with every entry on the gateway. The check covers the")
        lines.append("  whole page, so a save that enables filtering on one network can also hold up disabling it on")
        lines.append("  another.")
    return "\n".join(lines)


_PHASE_NOTES: dict[str, str] = {
    "services": "custom services (before the forwards that use them)",
    "apphosting": "NAT/Gaming forwards (a forward waits until its service appears in the dropdown)",
    "ipalloc": "fixed reservations (a reservation can need Allocate, then Save)",
    "packetfilter": "documentary only: recorded, never written",
    "dosprotect": "Firewall Advanced",
    "wconfig": "Advanced Wi-Fi",
    "wmacauth": "Wi-Fi MAC filtering modes (allow/deny need the recorded list on the gateway; the guard is page-wide)",
    "ippass": "IP Passthrough",
    "etherlan": "LAN ports (can cut the LAN port you are on)",
    "dhcpserver": "Subnets & DHCP, last (can move the gateway address)",
}


def restore_phases_epilog() -> str:
    return "\n".join([
        "Planning phases (the order restore plans pages, not the order writes are sent):",
        restore_phase_rows(),
        "  With --prune, router rows missing from the dump are removed too: --prune removes a forward before its",
        "  service, a page that still has an add pending defers its removes, and reservations are never released.",
        "  Live runs need --commit --confirm RESTORE.",
    ])


def actions_epilog() -> str:
    from .actions import ROUTER_ACTIONS

    name_width = max(len(action.name) for action in ROUTER_ACTIONS) + 2
    token_width = max(len(action.confirm_token) for action in ROUTER_ACTIONS) + len("--confirm ") + 2
    page_width = max(len(action.page) for action in ROUTER_ACTIONS) + 2
    lines = ["Actions (bgwcli action <name> previews; add --commit --confirm <token> to POST):"]
    for action in ROUTER_ACTIONS:
        guard = "dangerous" if action.dangerous else "guarded"
        aliases = f" (also: {', '.join(action.aliases)})" if action.aliases else ""
        lines.append(
            f"  {action.name.ljust(name_width)}{('--confirm ' + action.confirm_token).ljust(token_width)}"
            f"{action.page.ljust(page_width)}{guard.ljust(11)}{action.description.rstrip('.')}{aliases}"
        )
    return "\n".join(lines)


def page_list_epilog() -> str:
    lines = ["Pages (type the page id shown first; labels and \"Section/Tab\" paths also work):"]
    sections = list(dict.fromkeys(row.section for row in page_rows()))
    width = max(len(row.page) for row in page_rows()) + 2
    label_width = max(len(row.label) for row in page_rows()) + 2
    for section in sections:
        lines.append(f"  {section}:")
        for row in page_rows(section):
            marker = " [guarded]" if row.guarded else ""
            lines.append(f"    {row.page.ljust(width)}{row.label.ljust(label_width)}{row.summary}{marker}")
    lines.append("  A bare page name such as status is not section-scoped here; use the page id.")
    return "\n".join(lines)


def write_pages_epilog(command: str) -> str:
    from .mutations import DANGEROUS_PAGES

    by_page = {row.page: row for row in page_rows()}
    blocked = [(page, f"{by_page[page].section} / {by_page[page].label}") for page in sorted(DANGEROUS_PAGES)]
    if command == "set":
        rule = "set refuses these pages even as a dry run"
        checks = [
            "set posts the page's current form values plus your KEY=VALUE overrides with the page's Save button,",
            "and refuses a Save button the page renders disabled; a page with no Save button only gets a warning.",
        ]
    else:
        rule = "submit previews a button on these pages but refuses --commit"
        checks = [
            "submit presses the named button, which must be enabled. It is refused only when the page records the",
            "form that owns the button and none of its owner forms posts to this page's own CGI. A button that",
            "only opens an editor takes no KEY=VALUE.",
        ]
    return "\n".join([
        "Writes: bgwcli inspect <page> shows the fields and buttons of form pages (pages with a dedicated view,",
        "such as devices, show that view instead).",
        confirm_token_rule(),
        *checks,
        "",
        f"Guarded pages ({rule}; use bgwcli action where one exists):",
        _rows(blocked),
    ])


SESSION_EPILOG = "\n".join([
    "Subcommands:",
    "  status        show the local session cache and cooldown for this router (default)",
    "  clear-cache   delete the local session cache; it also removes the local cooldown, so use it only for",
    "                local-state troubleshooting, not as a retry shortcut (no router logout is sent)",
])


def command_epilog(name: str) -> str | None:
    """The generated epilog for a non-section command's help, or None."""
    if name == "dump":
        return dump_pages_epilog() + "\n\n" + exit_codes_epilog("dump")
    if name in ("diff", "autorestore"):
        return include_ids_epilog(name) + "\n\n" + exit_codes_epilog(name)
    if name == "restore":
        return "\n\n".join([include_ids_epilog(name), restore_phases_epilog(), exit_codes_epilog(name)])
    if name == "action":
        return actions_epilog()
    if name in ("page", "inspect"):
        return page_list_epilog()
    if name in ("set", "submit"):
        return write_pages_epilog(name)
    if name == "session":
        return SESSION_EPILOG
    return None


def id_list(registry: str) -> str:
    """Comma-separated ids of one registry, for prose in the root help."""
    if registry == "optional":
        from .snapshot import OPTIONAL_FORM_PAGES as ids
    elif registry == "restorable":
        from .snapshot_diff import RESTORABLE_PAGES as ids
    elif registry == "guarded":
        from .mutations import DANGEROUS_PAGES

        ids = tuple(sorted(DANGEROUS_PAGES))
    else:
        raise ValueError(registry)
    return ", ".join(ids)


# ---------------------------------------------------------------------------------------------
# README blocks (kept current by scripts/update_readme_pages.py; tests fail when they drift)


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def readme_pages_markdown() -> str:
    lines: list[str] = []
    for section in dict.fromkeys(row.section for row in page_rows()):
        lines += [f"### {section}", "", "| Command | Shows |", "| --- | --- |"]
        for row in page_rows(section):
            marker = " (default)" if row.is_default else ""
            marker += " (guarded)" if row.guarded else ""
            lines.append(f"| `bgwcli {row.command} {row.slug}` | {_md(row.summary)}{marker} |")
        if section == "Diagnostics":
            for name, summary in _DIAGNOSTIC_OPERATIONS:
                lines.append(f"| `bgwcli diagnostics {name}` | {summary}; dry run, `--commit --confirm DIAG` sends |")
        lines.append("")
    return "\n".join(lines)


def readme_include_markdown() -> str:
    from .snapshot import OPTIONAL_FORM_PAGES, snapshot_pages_for
    from .snapshot_diff import RESTORABLE_PAGES

    lines = ["Read by every `dump`:", "", "| Page id | What it adds to the dump |", "| --- | --- |"]
    lines += [f"| `{page}` | {_md(DUMP_CONTRIBUTIONS[page])} |" for page in snapshot_pages_for(())]
    lines += [
        "",
        "Optional, with `dump --include <id>[,<id>]` or `--include all`; capturing is read-only, the notes are the "
        "risks of restoring them:",
        "",
        "| Page id | What it captures; restore risk |",
        "| --- | --- |",
    ]
    lines += [f"| `{page}` | {_md(INCLUDE_RISK_NOTES[page])} |" for page in OPTIONAL_FORM_PAGES]
    lines += [
        "",
        f"`dump --include`: `all` = {', '.join(f'`{page}`' for page in OPTIONAL_FORM_PAGES)}; `dosprotect` and "
        "`wconfig` are accepted but always read; any other id is rejected. `diff`, `restore` and `autorestore` "
        f"`--include` take the restorable ids {', '.join(f'`{page}`' for page in RESTORABLE_PAGES)} (omitted or "
        "`all` = everything restorable in the dump; for `autorestore` the selection also limits reset detection).",
        "",
    ]
    return "\n".join(lines)


def readme_actions_markdown() -> str:
    from .actions import ROUTER_ACTIONS

    lines = ["| Action | Token | Source page | Kind | What it does |", "| --- | --- | --- | --- | --- |"]
    for action in ROUTER_ACTIONS:
        kind = "dangerous" if action.dangerous else "guarded"
        aliases = f" (also `{'`, `'.join(action.aliases)}`)" if action.aliases else ""
        lines.append(
            f"| `{action.name}` | `{action.confirm_token}` | `{action.page}` | {kind} | "
            f"{_md(action.description)}{aliases} |"
        )
    lines.append("")
    return "\n".join(lines)


README_BLOCKS: dict[str, str] = {
    "pages": "readme_pages_markdown",
    "include-pages": "readme_include_markdown",
    "actions": "readme_actions_markdown",
    "restore-phases": "readme_restore_phases_markdown",
    "backup-exits": "readme_backup_exits_markdown",
    "write-rules": "readme_write_rules_markdown",
}


def confirm_token_rule() -> str:
    """How set/submit derive their --confirm token (mutations.confirm_token_for_page), with a generated example."""
    from .mutations import confirm_token_for_page

    return (
        "Commit with --commit --confirm <TOKEN>: the page id in upper case, punctuation runs become hyphens, as "
        f"the dry run prints it (e.g. wconfig_unified -> {confirm_token_for_page('wconfig_unified')})."
    )


def restore_phase_rows(indent: str = "  ") -> str:
    from .restore import RESTORE_PAGE_ORDER

    width = max(len(page) for page in RESTORE_PAGE_ORDER) + 2
    rows = enumerate(RESTORE_PAGE_ORDER, 1)
    return "\n".join(f"{indent}{index:>2}. {page.ljust(width)}{_PHASE_NOTES[page]}" for index, page in rows)


def root_actions_list(indent: str = "      ") -> str:
    from .actions import ROUTER_ACTIONS

    width = max(len(action.name) for action in ROUTER_ACTIONS) + 1
    token_width = max(len(action.confirm_token) for action in ROUTER_ACTIONS) + len("--confirm=") + 2
    return "\n".join(
        f"{indent}{action.name.ljust(width)}{('--confirm=' + action.confirm_token).ljust(token_width)}"
        f"{'dangerous' if action.dangerous else 'guarded'}"
        for action in ROUTER_ACTIONS
    )


def snapshot_page_count() -> int:
    from .snapshot import SNAPSHOT_PAGES

    return len(SNAPSHOT_PAGES)


def readme_restore_phases_markdown() -> str:
    from .restore import RESTORE_PAGE_ORDER

    lines = ["| Phase | Page id | What restore does there |", "| --- | --- | --- |"]
    lines += [f"| {index} | `{page}` | {_md(_PHASE_NOTES[page])} |" for index, page in enumerate(RESTORE_PAGE_ORDER, 1)]
    lines += ["", "These are planning phases, not the order writes are sent: with `--prune` a forward is removed "
              "before its service, and a page with an add pending defers its removes.", ""]
    return "\n".join(lines)


def readme_backup_exits_markdown() -> str:
    lines = ["| Command | Code | Meaning |", "| --- | --- | --- |"]
    for command in ("dump", "diff", "restore", "autorestore"):
        lines += [f"| `{command}` | `{code}` | {_md(meaning)} |" for code, meaning in _EXIT_CODES[command]]
    lines.append("")
    return "\n".join(lines)


def readme_write_rules_markdown() -> str:
    from .mutations import DANGEROUS_PAGES, confirm_token_for_page

    guarded = ", ".join(f"`{page}`" for page in sorted(DANGEROUS_PAGES))
    return "\n".join([
        f"On the guarded pages ({guarded}) `set` refuses even a dry run, and `submit` previews a button on them "
        "but refuses `--commit`; use a named `action` where one exists (Restart and Resets have dangerous actions; "
        "Access Code and Update have no registered action).",
        "",
        "`set` and `submit` commit with `--commit --confirm <TOKEN>`: the page id in upper case, punctuation runs "
        "become hyphens, as the dry run prints it "
        f"(`wconfig_unified` → `{confirm_token_for_page('wconfig_unified')}`).",
        "",
        "A write is sent once, with one exception: if the gateway answers a configuration POST with its Login "
        "page, bgwcli logs in again and re-sends once, so two attempts can be reported.",
        "",
    ])
