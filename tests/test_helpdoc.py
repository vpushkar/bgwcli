"""Help text derived from the router tab map: page names, summaries and rendered help blocks."""

from __future__ import annotations

import json
from pathlib import Path

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


# ---------------------------------------------------------------------------------------------
# Task 2: command help, help <command>, option visibility (display only)

import argparse  # noqa: E402
import re  # noqa: E402

from bgwcli.errors import UsageError  # noqa: E402

ALL_GLOBALS = frozenset({
    "--host", "--config", "--access-code-stdin", "--json", "--include-secrets", "--timeout", "--strict-tls",
    "--wait-for-session", "--session-wait-timeout", "--session-wait-interval",
})
NO_SECRETS = ALL_GLOBALS - {"--include-secrets"}
PUBLIC_GET = frozenset({"--host", "--config", "--json", "--timeout", "--strict-tls"})
PAGE_VIEW = frozenset({"--raw", "--forms", "--limit"})
SWEEP = frozenset({"--delay", "--pages", "--include-parsed", "--full", "--forms", "--raw", "--out"})
COMMIT = frozenset({"--commit", "--confirm"})

# Literal expectation (not imported from helpdoc): visible global + command options per command.
EXPECTED_VISIBLE: dict[str, frozenset[str]] = {
    "check": NO_SECRETS,
    "auth": NO_SECRETS,
    "sitemap": PUBLIC_GET,
    "coverage": PUBLIC_GET,
    "tabs": frozenset({"--config", "--json"}),
    "actions": frozenset({"--config", "--json"}),
    "section": frozenset({"--config", "--json"}),
    "session": frozenset({"--host", "--config", "--json"}),
    "help": frozenset(),
    "action": ALL_GLOBALS | COMMIT,
    "sweep": ALL_GLOBALS | SWEEP,
    "scan": ALL_GLOBALS | SWEEP,
    "schema": ALL_GLOBALS | {"--delay", "--pages", "--raw", "--out"},
    "audit": ALL_GLOBALS | {"--delay", "--pages"},
    "readiness": ALL_GLOBALS | {"--delay", "--pages"},
    "device": ALL_GLOBALS | PAGE_VIEW | {"--all"},
    "broadband": ALL_GLOBALS | PAGE_VIEW,
    "home-network": ALL_GLOBALS | PAGE_VIEW,
    "voice": ALL_GLOBALS | PAGE_VIEW,
    "firewall": ALL_GLOBALS | PAGE_VIEW,
    "diagnostics": ALL_GLOBALS | PAGE_VIEW | COMMIT | {"--ipv4", "--ipv6"},
    "page": ALL_GLOBALS | PAGE_VIEW | {"--all"},
    "inspect": ALL_GLOBALS | {"--raw", "--limit", "--all"},
    "devices": NO_SECRETS | {"--raw", "--limit", "--all"},
    "wifi": ALL_GLOBALS | PAGE_VIEW,
    "nat": ALL_GLOBALS | PAGE_VIEW,
    "logs": NO_SECRETS | {"--raw", "--limit"},
    "set": ALL_GLOBALS | COMMIT,
    "submit": ALL_GLOBALS | COMMIT,
    "status": ALL_GLOBALS,
    "dump": NO_SECRETS | {"--out", "--include", "--all-clients"},
    "diff": ALL_GLOBALS | {"--include"},
    "restore": ALL_GLOBALS | {"--prune", "--include"} | COMMIT,
    "autorestore": NO_SECRETS | {"--include", "--max-passes", "--wait", "--on-any-diff"} | COMMIT,
    "fixtures-capture": NO_SECRETS | {"--out", "--delay", "--pages"},
}
SECTION_ALIASES = {"home-network": ("home", "lan"), "diagnostics": ("diag", "diagnostic")}


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _option_lines(help_text: str) -> dict[str, str]:
    """Option-definition lines only (`  --flag <metavar>   help`), keyed by each long flag on the line."""
    lines: dict[str, str] = {}
    for line in help_text.splitlines():
        if line.startswith("  -"):
            for token in line.split():
                if token.startswith("--"):
                    lines[token.rstrip(",")] = line
                elif not token.startswith("-") and not token.startswith("<"):
                    break
    return lines


def _section_help(capsys, argv) -> str:
    if argv[0] == "help":
        assert cli.main(argv) == 0
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 0
    return capsys.readouterr().out


def _page_block(text: str) -> str:
    return text[text.index("Pages (bgwcli "):]


def test_parser_builds_and_root_help_works(capsys):
    assert cli.main(["-h"]) == 0
    assert capsys.readouterr().out == cli.help_text()


_HELP_FORMS = [
    form
    for command in cli.SECTION_COMMANDS
    for name in (command, *SECTION_ALIASES.get(command, ()))
    for form in ([name, "-h"], [name, "--help"], ["help", name])
]


@pytest.mark.parametrize("argv", _HELP_FORMS, ids=[" ".join(form) for form in _HELP_FORMS])
def test_section_help_lists_every_page(capsys, argv):
    text = _section_help(capsys, argv)
    typed = argv[-1] if argv[0] == "help" else argv[0]
    command = cli._ALIAS_TO_COMMAND.get(typed, typed)
    block = _page_block(text)
    rows = [row for row in helpdoc.page_rows() if row.command == command]
    assert rows
    lines = block.splitlines()
    for row in rows:
        matching = [line for line in lines if line.split()[:1] == [row.slug]]
        assert len(matching) == 1, row.slug
        assert row.summary in matching[0]
    assert not any(mark in block for mark in ELLIPSES)


def test_device_help_names_firmware(capsys):
    text = _section_help(capsys, ["device", "-h"])
    assert "system-information" in text
    assert "Software Version" in text


def test_section_rows_survive_narrow_terminal(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    block = _page_block(_section_help(capsys, ["home-network", "-h"]))
    lines = [line.split() for line in block.splitlines() if line.startswith("  ") and line.split()]
    rows = helpdoc.page_rows("Home Network")
    assert rows
    for row in rows:
        assert [row.slug, *row.summary.split()] in [line[: 1 + len(row.summary.split())] for line in lines], row.slug


def test_no_parser_has_generic_positional_help():
    for name, sub in _subparsers().items():
        assert "positional arguments for the command" not in sub.format_help(), name


@pytest.mark.parametrize("command", sorted(EXPECTED_VISIBLE))
def test_visible_options_per_command(command):
    sub = _subparsers()[command]
    shown = set(_option_lines(sub.format_help())) - {"--help"}
    assert shown == set(EXPECTED_VISIBLE[command])


def test_dump_out_option_shows_a_file():
    lines = _option_lines(_subparsers()["dump"].format_help())
    assert "<file>" in lines["--out"]


_PARSER_MANIFEST = Path(__file__).resolve().parent / "data" / "parser_manifest_50eafa4.json"


def _action_records(parser: argparse.ArgumentParser) -> list[dict[str, object]]:
    """Behavioural fingerprint of every action (help strings and metavars are excluded on purpose)."""
    records = [
        {"option_strings": list(a.option_strings), "dest": a.dest, "action": type(a).__name__,
         "const": repr(a.const), "nargs": repr(a.nargs), "default": repr(a.default)}
        for a in parser._actions
    ]
    return sorted(records, key=lambda record: json.dumps(record, sort_keys=True))


def _parser_manifest() -> dict[str, list[dict[str, object]]]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    groups: dict[int, tuple[argparse.ArgumentParser, list[str]]] = {}
    for name, sub in action.choices.items():
        groups.setdefault(id(sub), (sub, []))[1].append(name)
    manifest = {"<root>": _action_records(parser)}
    for sub, names in groups.values():
        manifest["|".join(sorted(names))] = _action_records(sub)
    return manifest


def test_parser_surface_unchanged():
    base = json.loads(_PARSER_MANIFEST.read_text(encoding="utf-8"))
    current = _parser_manifest()
    version = [r for r in current["<root>"] if r["option_strings"] == ["--version"]]
    assert len(version) == 1, "the root parser must gain exactly one --version action"
    current["<root>"] = [r for r in current["<root>"] if r is not version[0]]
    for name, records in current.items():
        if name != "<root>":
            assert not any("--version" in r["option_strings"] for r in records), name
    assert sorted(current) == sorted(base)
    for name in base:
        assert current[name] == base[name], name
    seen_actions: dict[int, str] = {}
    distinct = {id(sub): (name, sub) for name, sub in _subparsers().items()}
    for name, sub in distinct.values():
        for action in sub._actions:
            assert seen_actions.setdefault(id(action), name) == name, (name, action.option_strings)


def test_hidden_option_still_parses(tmp_env):
    command = cli.parse_args(["device", "system", "--prune", "information"])
    assert command.args == ["system", "information"]
    assert command.prune is True
    assert cli.parse_args(["--limit", "5", "device"]).limit == 5
    assert cli.parse_args(["--limit", "5", "device", "--limit", "7"]).limit == 7
    assert cli.parse_args(["device", "--ipv4", "--ipv6"]).protocol == "IPv6"
    assert cli.parse_args(["diagnostics", "--ipv6", "--ipv4"]).protocol == "IPv4"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["device", "--lim", "5"], "unrecognized arguments: --lim"),
        (["device", "--timeout", "x"], "--timeout"),
        (["device", "--out", "x"], "--out is not used by device"),
    ],
)
def test_rejections_unchanged(tmp_env, capsys, argv, message):
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().err


def test_root_timeout_without_command_still_prints_help(tmp_env, capsys):
    assert cli.main(["--timeout", "x"]) == 0
    assert capsys.readouterr().out == cli.help_text()


@pytest.mark.parametrize("argv", [["help", "nope"], ["-h", "device"]])
def test_help_unknown_target_prints_root_help(capsys, argv):
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == cli.help_text()


def test_help_forms_identical_when_parse_succeeds(capsys):
    via_help = _section_help(capsys, ["help", "device"])
    via_flag = _section_help(capsys, ["device", "-h"])
    assert via_help == via_flag


def test_help_command_keeps_environment_validation_order(tmp_env, capsys, monkeypatch):
    monkeypatch.setenv("BGW_LIMIT", "bad")
    assert cli.main(["help", "device"]) == 1
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main(["device", "-h"])
    assert exc.value.code == 0


def test_metadata_covers_every_parser():
    names = set(_subparsers()) - {alias for aliases in cli.SECTION_COMMANDS.values() for alias in aliases}
    assert set(helpdoc.COMMAND_ARGS) == names
    assert set(helpdoc.COMMAND_OPTION_USE) == names
    assert set(helpdoc.GLOBAL_OPTION_USE) == names


def test_usage_error_type_is_unchanged():
    with pytest.raises(UsageError):
        cli.parse_args(["device", "--lim", "5"])


# ---------------------------------------------------------------------------------------------
# Task 3: root help, tabs, section


DATA = Path(__file__).resolve().parent / "data"


def _root_pages_block() -> str:
    text = cli.help_text()
    start = text.index("Router pages (bgwcli <section> <page>")
    return text[start:text.index("\n\n", text.index("bgwcli diagnostics", start))]


def test_root_help_lists_every_page_with_summary():
    block = _root_pages_block()
    lines = block.splitlines()
    headers = [index for index, line in enumerate(lines) if line.startswith("  bgwcli ")]
    commands = [lines[index].split()[1] for index in headers]
    assert commands == list(cli.SECTION_COMMANDS)
    for position, index in enumerate(headers):
        end = headers[position + 1] if position + 1 < len(headers) else len(lines)
        section_lines = lines[index + 1:end]
        for tab in ROUTER_TABS:
            if helpdoc.section_command(tab.section) != commands[position]:
                continue
            matching = [line for line in section_lines if line.split()[:1] == [helpdoc.tab_slug(tab)]]
            assert len(matching) == 1, tab.label
            assert helpdoc.TAB_SUMMARIES[tab.page] in matching[0]
    assert not any(mark in block for mark in ELLIPSES)


def _tabs_text(capsys, argv) -> list[str]:
    assert cli.main(argv) == 0
    return capsys.readouterr().out.splitlines()


def _assert_one_row_per_tab(lines, tabs):
    assert len(lines) == len(tabs)
    for tab in tabs:
        command = f"bgwcli {helpdoc.section_command(tab.section)} {helpdoc.tab_slug(tab)}"
        matching = [line for line in lines if f" {tab.page} " in f" {line} " and command in line]
        assert len(matching) == 1, tab.label
        line = matching[0]
        assert helpdoc.TAB_SUMMARIES[tab.page] in line
        assert line == line.rstrip()
        assert line.split()[-1] == "guarded" if tab.dangerous else line.split()[-1] != "guarded"


def test_tabs_text_has_one_row_per_tab(capsys, tmp_env):
    _assert_one_row_per_tab(_tabs_text(capsys, ["tabs"]), ROUTER_TABS)


@pytest.mark.parametrize("section", ["device", "broadband", "home-network", "voice", "firewall", "diagnostics"])
def test_section_text_has_one_row_per_tab(capsys, tmp_env, section):
    tabs = [tab for tab in ROUTER_TABS if helpdoc.section_command(tab.section) == section]
    _assert_one_row_per_tab(_tabs_text(capsys, ["section", section]), tabs)


def test_tabs_text_keeps_first_column_minimum_width(capsys, tmp_env):
    voice = [line for line in _tabs_text(capsys, ["section", "voice"])]
    assert all(line.startswith("Voice    ") for line in voice)  # "Voice" padded to the 7-wide minimum + 2


def test_tabs_json_byte_identical(capsys, tmp_env):
    assert cli.main(["tabs", "--json"]) == 0
    assert capsys.readouterr().out == (DATA / "tabs_json_50eafa4.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("section", ["device", "broadband", "home-network", "voice", "firewall", "diagnostics"])
def test_section_json_byte_identical(capsys, tmp_env, section):
    assert cli.main(["section", section, "--json"]) == 0
    expected = (DATA / f"section_{section}_json_50eafa4.json").read_text(encoding="utf-8")
    assert capsys.readouterr().out == expected


# ---------------------------------------------------------------------------------------------
# Task 3b: backup, write and fetch command help, generated from the registries the code enforces

from bgwcli import mutations, pages, restore, snapshot, snapshot_diff  # noqa: E402
from bgwcli.actions import ROUTER_ACTIONS, get_action  # noqa: E402


def _help(command: str) -> str:
    text = cli.command_help(command)
    assert text is not None
    return text


def _rows_starting_with(text: str, word: str) -> list[str]:
    return [line for line in text.splitlines() if line.split()[:1] == [word]]


def test_dump_help_lists_every_default_read():
    text = _help("dump")
    for page in snapshot.snapshot_pages_for(()):
        rows = _rows_starting_with(text, page)
        assert len(rows) == 1, page
    assert "firmware version only" in _rows_starting_with(text, "sysinfo")[0]
    assert "best effort" in _rows_starting_with(text, "packetfilter")[0]
    for page in snapshot.OPTIONAL_FORM_PAGES:
        assert len(_rows_starting_with(text, page)) == 1, page
    assert set(helpdoc.INCLUDE_RISK_NOTES) == set(snapshot.OPTIONAL_FORM_PAGES)


@pytest.mark.parametrize(
    ("selector", "outcome"),
    [("all", "accepted"), ("etherlan", "accepted"), ("dosprotect", "redundant"), ("services", "rejected")],
)
def test_dump_selector_rules_match_resolver(selector, outcome):
    text = " ".join(_help("dump").split())
    if outcome == "rejected":
        with pytest.raises(UsageError):
            snapshot.resolve_include(selector)
        assert "any other id (e.g. services) is rejected" in text
    elif outcome == "redundant":
        assert snapshot.resolve_include(selector) == ()
        assert "dosprotect and wconfig are accepted but always read" in text
    else:
        assert snapshot.resolve_include(selector)
        assert "all = etherlan, dhcpserver, ippass, wmacauth" in text


@pytest.mark.parametrize("command", ["diff", "restore", "autorestore"])
def test_include_ids_listed(command):
    text = _help(command)
    for page in snapshot_diff.RESTORABLE_PAGES:
        assert _rows_starting_with(text, page), page
    flat = " ".join(text.split())
    assert "omitted or all = every restorable page present in the dump" in flat
    if command == "autorestore":
        assert "also limits which pages count toward reset detection" in flat


def test_restore_help_phases():
    text = _help("restore")
    block = text[text.index("Planning phases"):text.index("Exit codes:")]
    numbered = [line.split() for line in block.splitlines() if line.strip()[:1].isdigit()]
    assert [parts[1] for parts in numbered] == list(restore.RESTORE_PAGE_ORDER)
    assert [parts[0] for parts in numbered] == [f"{n}." for n in range(1, len(numbered) + 1)]
    flat = " ".join(block.split())
    assert "not the order writes are sent" in flat
    assert "--prune removes a forward before its service" in flat
    packetfilter = next(line for line in block.splitlines() if " packetfilter " in line)
    assert "never written" in packetfilter
    assert "can move the gateway address" in flat


def test_action_help_rows():
    text = _help("action")
    for action in ROUTER_ACTIONS:
        rows = _rows_starting_with(text, action.name)
        assert len(rows) == 1, action.name
        row = rows[0]
        fields = row.split()
        if action.requires_confirmation:
            assert fields[fields.index("--confirm") + 1] == action.confirm_token, action.name
        else:
            assert "--confirm" not in fields and "--commit only" in row, action.name
        assert f" {action.page} " in f" {row} "
        assert action.description.rstrip(".") in row
        assert ("dangerous" in row.split()) == bool(action.dangerous), action.name
        for alias in action.aliases or ():
            assert get_action(alias) is action, alias
            assert f"(also: {', '.join(action.aliases)})" in row, alias


@pytest.mark.parametrize("command", ["page", "inspect"])
def test_page_help_arguments_resolve(command):
    text = _help(command)
    for tab in ROUTER_TABS:
        rows = _rows_starting_with(text, tab.page)
        assert len(rows) == 1, tab.page
        assert pages.resolve_cgi_page(rows[0].split()[0]) == tab.page
        assert helpdoc.TAB_SUMMARIES[tab.page] in rows[0]


def test_dangerous_sets_agree():
    assert pages.dangerous_pages() == mutations.DANGEROUS_PAGES


@pytest.mark.parametrize("command", ["set", "submit"])
def test_write_help_rules(command):
    text = _help(command)
    for page in sorted(mutations.DANGEROUS_PAGES):
        assert _rows_starting_with(text, page), page
    flat = " ".join(text.split())
    assert "punctuation runs become hyphens" in flat
    if command == "set":
        assert "set refuses these pages even as a dry run" in flat
    else:
        assert "submit previews a button on these pages but refuses --commit" in flat


RESTORE_EXIT_0 = (
    "0    --commit: the closing diff finds no selected dumped service, forward, reservation or form value still "
    "missing or different (--prune: and no extra service or forward); form fields the live page disables or does "
    "not render, controls only on the router, and extra reservations are ignored"
)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("dump", [
            "0    dump file written",
            "1    usage error (e.g. an unknown --include id)",
            "2    the router or a page could not be read, or the snapshot could not be built",
            "2    the --out destination is unusable or could not be written",
            "130  interrupted (Ctrl-C)",
        ]),
        ("diff", [
            "0    identical (a firmware-only change is still 0)",
            "1    differences found, or none of the --include pages is in the dump",
            "2    the dump file or the router could not be read",
            "130  interrupted (Ctrl-C)",
        ]),
        ("restore", [
            "0    dry run: plan printed (even when work remains)",
            RESTORE_EXIT_0,
            "1    not converged, a write rejected, confirmation refused, or nothing to compare",
            "2    the dump file or the router could not be read",
            "2    the allocation preflight is blocked (a fixed allocation held by another MAC, no enabled Clear and "
            "Rescan button, or a failed preflight or rescan), also in a dry run",
            "2    a write was sent and never acknowledged (even when the closing diff is readable)",
            "130  interrupted (Ctrl-C); the write state is reported",
        ]),
        ("autorestore", [
            "0    no reset, converged, or router unreachable (also during the allocation preflight, before any write)",
            "1    a restore is needed (dry run) or did not converge (the next timer run retries)",
            "2    page-level 401/403 or an initial structural failure",
            "2    the allocation preflight was blocked by a conflict or failed for another reason",
            "2    a write was sent and never acknowledged, or the closing verification failed",
            "2    a recovery checkpoint could not be read or written, another unfinished recovery is recorded, or the "
            "same failure ended three consecutive runs",
            "130  interrupted (Ctrl-C)",
        ]),
    ],
)
def test_exit_code_pairs(command, expected):
    text = _help(command)
    section = text[text.index("Exit codes:"):]
    section = section.split("\n\n")[0]
    rows = [" ".join(line.split()) for line in section.splitlines()[1:]]
    assert rows == [" ".join(row.split()) for row in expected]


def test_root_help_backup_prose_preserved():
    text = " ".join(cli.help_text().split())
    for needle in ("dump --include all", "--include <csv|all>", "reservations are never released",
                   "bgwcli dump -h lists", "bgwcli action -h lists every action"):
        assert needle in text, needle
    assert "etherlan (LAN ports), dhcpserver (Subnets & DHCP)" not in text  # literal list gone


def test_actions_text_aligned(capsys, tmp_env):
    assert cli.main(["actions"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(ROUTER_ACTIONS)
    assert len({line.index("confirm=") for line in lines}) == 1
    descriptions = {line.index(action.description) for line, action in zip(lines, ROUTER_ACTIONS, strict=True)}
    assert len(descriptions) == 1


def test_actions_json_byte_identical(capsys, tmp_env):
    assert cli.main(["actions", "--json"]) == 0
    assert capsys.readouterr().out == (DATA / "actions_json_50eafa4.json").read_text(encoding="utf-8")


def test_session_help_caveat():
    flat = " ".join(_help("session").split())
    assert "clear-cache" in flat and "also removes the local cooldown" in flat


# ---------------------------------------------------------------------------------------------
# Task 4: README blocks generated from the same sources

README = Path(__file__).resolve().parent.parent / "README.md"
_BLOCKS = {"pages": "readme_pages_markdown", "include-pages": "readme_include_markdown",
           "actions": "readme_actions_markdown", "restore-phases": "readme_restore_phases_markdown",
           "backup-exits": "readme_backup_exits_markdown", "write-rules": "readme_write_rules_markdown"}


def _readme_block(name: str) -> str:
    text = README.read_text(encoding="utf-8")
    start, end = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
    assert start in text and end in text, f"README is missing the {name} markers"
    return text[text.index(start) + len(start):text.index(end)]


@pytest.mark.parametrize("name", sorted(_BLOCKS))
def test_readme_blocks_are_current(name):
    expected = getattr(helpdoc, _BLOCKS[name])()
    assert _readme_block(name) == expected, "README is stale: run .venv/bin/python -I scripts/update_readme_pages.py"


def test_readme_pages_block_lists_every_tab_under_its_section():
    block = _readme_block("pages")
    for tab in ROUTER_TABS:
        heading = block.index(f"### {tab.section}\n")
        following = block.find("\n### ", heading + 1)
        section = block[heading:following if following != -1 else len(block)]
        command = f"`bgwcli {helpdoc.section_command(tab.section)} {helpdoc.tab_slug(tab)}`"
        assert section.count(command) == 1, command
    assert "`bgwcli diagnostics site-map`" in block


def _include_table_ids(block: str, heading: str) -> list[str]:
    """Page ids (first backticked cell) of the table that follows the line starting with ``heading``."""
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    ids: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("| `"):
            ids.append(line.split("`")[1])
        elif ids and not line.startswith("|"):
            break
    return ids


def test_readme_include_block_lists_every_selector():
    block = _readme_block("include-pages")
    assert _include_table_ids(block, "Read by every `dump`") == list(snapshot.snapshot_pages_for(()))
    assert _include_table_ids(block, "Optional, with `dump --include") == list(snapshot.OPTIONAL_FORM_PAGES)
    for page in snapshot_diff.RESTORABLE_PAGES:
        assert f"`{page}`" in block, page


def test_readme_actions_block_lists_every_action():
    block = _readme_block("actions")
    for action in ROUTER_ACTIONS:
        row = next(line for line in block.splitlines() if line.startswith(f"| `{action.name}` |"))
        token = f"`{action.confirm_token}`" if action.requires_confirmation else "| — |"
        assert token in row and f"`{action.page}`" in row


def test_readme_states_submit_and_set_rules_and_firmware_command():
    text = " ".join(README.read_text(encoding="utf-8").split())
    assert "`submit` previews a button on them but refuses `--commit`" in text
    assert "Access Code and Update have no registered action" in text
    assert "bgwcli device system-information" in text and "Software Version" in text
    assert "`bgwcli <section> -h` lists that section's pages" in text


# ---------------------------------------------------------------------------------------------
# Task 5: bgwcli --version

import bgwcli  # noqa: E402


@pytest.fixture
def no_router_no_config(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("--version must not build a client or read configuration")

    monkeypatch.setattr(cli, "_client_factory", refuse)
    monkeypatch.setattr(cli, "load_config_file", refuse)


@pytest.mark.parametrize("argv", [["--version"], ["--bogus", "--version"], ["--version", "device"]])
def test_version_flag(capsys, no_router_no_config, argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"bgwcli {bgwcli.__version__}\n"
    assert captured.err == ""


def test_version_only_on_root(capsys, tmp_env):
    assert cli.main(["device", "--version"]) == 1
    assert "unrecognized arguments: --version" in capsys.readouterr().err


def test_help_mentions_version():
    text = " ".join(cli.help_text().split())
    assert "--version Print the bgwcli version and exit (before the command only)" in text


def test_version_is_0_2_0():
    assert bgwcli.__version__ == "0.4.0"


def test_readme_install_mentions_version_check():
    assert "Check what is installed with `bgwcli --version`" in README.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Final review fixes (each written to fail first)


def _exit_section(command: str) -> str:
    text = _help(command)
    return " ".join(text[text.index("Exit codes:"):].split())


def test_final_dump_out_failures_are_exit_2():
    section = _exit_section("dump")
    assert "2 the --out destination is unusable or could not be written" in section
    assert "--out" not in section[:section.index("2 ")]


def test_final_restore_exit_0_names_the_selected_scope():
    assert " ".join(RESTORE_EXIT_0.split()) in _exit_section("restore")


def test_final_autorestore_exit_rows_match_handlers():
    section = _exit_section("autorestore")
    assert "0 no reset, converged, or router unreachable (also during the allocation preflight, before any write)" \
        in section
    assert "2 a write was sent and never acknowledged, or the closing verification failed" in section
    assert "2 the allocation preflight was blocked by a conflict or failed for another reason" in section


@pytest.mark.parametrize("command", ["dump", "autorestore", "logs", "devices"])
def test_final_include_secrets_hidden_where_it_changes_nothing(command):
    assert "--include-secrets" not in _option_lines(_help(command))


def test_final_dump_help_states_secrets_in_the_file():
    flat = " ".join(_help("dump").split())
    assert "The dump file keeps Wi-Fi keys and other secrets in clear text (owner-only file), with or without " \
           "--include-secrets" in flat


def test_final_readme_redaction_promise_names_the_exceptions():
    text = " ".join(README.read_text(encoding="utf-8").split())
    assert "redacted on every output path unless" not in text
    assert "except `--raw` output, `--out` artifacts and dump files, which keep them (files are owner-only)" in text


@pytest.mark.parametrize("command", ["set", "submit"])
def test_final_confirm_token_rule_matches_the_generator(command):
    flat = " ".join(_help(command).split())
    token = mutations.confirm_token_for_page("wconfig_unified")
    assert token == "WCONFIG-UNIFIED"
    assert f"wconfig_unified -> {token}" in flat
    assert "punctuation runs become hyphens" in flat
    assert "WCONFIG_UNIFIED" not in flat


def test_final_root_help_confirm_token_rule():
    flat = " ".join(cli.help_text().split())
    assert "wconfig_unified -> WCONFIG-UNIFIED" in flat
    assert "<PAGE ID in upper case>" not in flat


@pytest.mark.parametrize("command", ["set", "submit"])
def test_final_write_help_does_not_overclaim(command):
    flat = " ".join(_help(command).split())
    assert "must be an enabled button of the page's own form" not in flat
    assert "pages with a dedicated view, such as devices, show that view instead" in flat


def test_final_mac_filter_note_keeps_both_cases():
    for text in (helpdoc.INCLUDE_RISK_NOTES["wmacauth"], _help("restore"), README.read_text(encoding="utf-8")):
        flat = " ".join(text.split())
        assert "mode none" in flat and "nonempty" in flat, flat[:80]


def test_final_root_help_lists_phases_actions_and_snapshot_count():
    text = cli.help_text()
    phases = [line.split()[1] for line in text.splitlines() if re.match(r"^\s+\d+\. ", line)]
    assert phases == list(restore.RESTORE_PAGE_ORDER)
    for action in ROUTER_ACTIONS:
        token = [f"--confirm={action.confirm_token}"] if action.requires_confirmation else ["--commit", "only"]
        assert any(line.split()[: 1 + len(token)] == [action.name, *token]
                   for line in text.splitlines()), action.name
    flat = " ".join(text.split())
    assert "captures every page" not in flat
    assert f"captures all {len(snapshot.SNAPSHOT_PAGES)} snapshot pages" in flat


def test_final_readme_has_generated_phases_exit_codes_and_write_rules():
    phases = _readme_block("restore-phases")
    rows = [line for line in phases.splitlines() if line.startswith("| ")][2:]
    assert [row.split("`")[1] for row in rows] == list(restore.RESTORE_PAGE_ORDER)
    exits = _readme_block("backup-exits")
    for command in ("dump", "diff", "restore", "autorestore"):
        assert f"`{command}`" in exits
    rules = " ".join(_readme_block("write-rules").split())
    for page in sorted(mutations.DANGEROUS_PAGES):
        assert f"`{page}`" in rules
    assert "logs in again and re-sends once" in rules


def _option_help(command: str, flag: str) -> str:
    """The full (wrapped) help of one option, whitespace-normalised."""
    action = next(a for a in _subparsers()[command]._actions if flag in a.option_strings)
    return " ".join(str(action.help).split())


def test_sweep_scan_forms_help_is_truthful():
    for command in ("sweep", "scan"):
        text = _option_help(command, "--forms")
        assert "terminal output" not in text
        assert "JSON" in text and "counts" in text


@pytest.mark.parametrize("command", ["sweep", "scan", "schema", "audit", "readiness"])
def test_pages_help_points_to_tabs_for_ids(command):
    text = _option_help(command, "--pages")
    assert "bgwcli tabs" in text and "page id" in text


def test_fixtures_capture_out_help_is_accurate():
    text = _option_help("fixtures-capture", "--out")
    assert "refuse" not in text and "fixture" in text


def test_help_command_finds_hidden_fixtures_capture(capsys):
    assert cli.command_help("fixtures-capture") == _subparsers()["fixtures-capture"].format_help()
    assert cli.main(["help", "fixtures-capture"]) == 0
    assert capsys.readouterr().out.startswith("usage: bgwcli fixtures-capture")
    assert "fixtures-capture" not in cli.help_text().split("Commands:", 1)[-1].split("\n\n", 1)[0]


def test_root_help_advertises_help_command_and_root_only_version():
    text = " ".join(cli.help_text().split())
    assert "bgwcli help <command>" in text and "bgwcli <command> -h" in text
    assert "--version" in text and "before the command" in text


# ---------------------------------------------------------------------------------------------
# Wording that states exactly what the code enforces (reviewer round: items 1, 3, 4, 5)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_restore_exit_0_does_not_claim_everything_selected_is_on_the_router():
    section = _exit_section("restore")
    assert "everything selected is on the router" not in section
    assert "extra reservations are ignored" in section


def test_root_help_restore_and_autorestore_do_not_claim_whole_dump_convergence():
    flat = _flat(cli.help_text())
    assert "everything in the dump is present" not in flat
    assert "the allocation preflight could not complete" not in flat
    # the same exit rows the command help renders
    assert _flat(RESTORE_EXIT_0) in flat
    assert "2 the allocation preflight is blocked (a fixed allocation held by another MAC" in flat
    assert "0 no reset, converged, or router unreachable (also during the allocation preflight, before any write)" \
        in flat


def test_set_and_submit_help_state_their_own_checks_separately():
    set_help = _flat(_help("set"))
    submit_help = _flat(_help("submit"))
    for text in (set_help, submit_help):
        assert "a button that belongs only to another form on the page is refused" not in text
        assert "pages with a dedicated view, such as devices, show that view instead" in text
    assert "set posts the page's current form values plus your KEY=VALUE overrides with the page's Save " \
           "button, and refuses a Save button the page renders disabled" in set_help
    assert "submit presses the named button, which must be enabled" in submit_help
    assert "refused only when the page records the form that owns the button and none of its owner forms " \
           "posts to this page's own CGI" in submit_help
    assert "A button that only opens an editor takes no KEY=VALUE" in submit_help


def test_root_help_inspect_names_pages_with_a_dedicated_view():
    flat = _flat(cli.help_text())
    assert "Pages with a dedicated view (home, lanstatistics, securityoptions, devices, logs) show that view " \
           "instead; --raw returns the router HTML." in flat


def test_mac_filter_wording_states_the_page_wide_guard():
    expected = (
        "disabling filtering (mode none) needs no recorded list; enabling allow/deny on any network needs a "
        "nonempty recorded list with every entry on the gateway, and the check covers the whole page, so a save "
        "that enables filtering on one network can also hold up disabling it on another"
    )
    assert expected in _flat(helpdoc.INCLUDE_RISK_NOTES["wmacauth"])
    for command in ("restore", "autorestore"):
        flat = _flat(_help(command))
        assert "restoring mode none (disable) needs no recorded list" in flat
        assert "needs a nonempty recorded list with every entry on the gateway" in flat
        assert "The check covers the whole page" in flat
        assert "can also hold up disabling it on another" in flat
    assert expected in _flat(README.read_text(encoding="utf-8"))
    assert "restore only when the dump recorded a nonempty list" not in _flat(README.read_text(encoding="utf-8"))


def test_readme_restore_walkthrough_qualifies_every_write_is_sent_once():
    flat = _flat(README.read_text(encoding="utf-8"))
    assert "Every write is sent once" not in flat
    assert "Each write is sent once, except that a Login-page answer to a configuration POST is followed by " \
           "one re-send after logging in again" in flat
    assert "the exit code is 0 only when everything in the dump is on the router" not in flat
    assert "the exit code is 0 only when the closing diff shows nothing the restore still owes" in flat


def test_readme_committed_advice_describes_each_evidence_field():
    flat = _flat(README.read_text(encoding="utf-8"))
    assert "an answer the CLI could not read is exit 2 with `committed` in the JSON telling you whether the " \
           "write reached the router" not in flat
    for needle in (
        "`committed: true` means the gateway acknowledged the save (`Changes saved`)",
        "`writeAttempted` says the POST was sent",
        "`writeResponseReceived` says the gateway answered it with any status",
        "`writeAttempts` is 2 after a re-send and absent otherwise",
        "a key whose answer is unknown is absent",
        "`committed: false` does not prove the write did not land",
        "re-read the gateway state (for example `bgwcli page <page>`) before retrying",
    ):
        assert needle in flat, needle
