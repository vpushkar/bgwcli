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


# ---------------------------------------------------------------------------------------------
# Task 2: command help, help <command>, option visibility (display only)

import argparse  # noqa: E402

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
    "tabs": frozenset({"--json"}),
    "actions": frozenset({"--json"}),
    "section": frozenset({"--json"}),
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
    "devices": ALL_GLOBALS | {"--raw", "--limit", "--all"},
    "wifi": ALL_GLOBALS | PAGE_VIEW,
    "nat": ALL_GLOBALS | PAGE_VIEW,
    "logs": ALL_GLOBALS | {"--raw", "--limit"},
    "set": ALL_GLOBALS | COMMIT,
    "submit": ALL_GLOBALS | COMMIT,
    "status": ALL_GLOBALS,
    "dump": ALL_GLOBALS | {"--out", "--include", "--all-clients"},
    "diff": ALL_GLOBALS | {"--include"},
    "restore": ALL_GLOBALS | {"--prune", "--include"} | COMMIT,
    "autorestore": ALL_GLOBALS | {"--include", "--max-passes", "--wait", "--on-any-diff"} | COMMIT,
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
    starts = [line.split()[0] for line in block.splitlines() if line.startswith("  ") and line.split()]
    for row in helpdoc.page_rows("Home Network"):
        assert row.slug in starts


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


def test_parser_surface_unchanged():
    expected = {flag for flags, _ in (*cli._GLOBAL_OPTIONS, *cli._COMMAND_OPTIONS) for flag in flags}
    expected |= {"-h", "--help"}
    seen_actions: dict[int, str] = {}
    distinct = {id(sub): (name, sub) for name, sub in _subparsers().items()}
    for name, sub in distinct.values():
        registered = {flag for action in sub._actions for flag in action.option_strings}
        assert registered == expected, name
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

from pathlib import Path  # noqa: E402

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
