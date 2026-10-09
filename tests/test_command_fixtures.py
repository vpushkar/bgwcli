"""Ported from tests/command-fixtures.test.ts: human tab commands resolve to captured fixture pages,
and the (gitignored) fixture pack proves diagnostics inputs/actions and scriptable summaries.
Fixture-backed assertions skip when tests/fixtures/{expected,parsed} are absent."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from bgwcli.types import (
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedLink,
    ParsedPage,
    ParsedSelect,
    ParsedTextarea,
    ParsedValueEntry,
    SelectOption,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
EXPECTED_DIR = FIXTURE_ROOT / "expected"
PARSED_DIR = FIXTURE_ROOT / "parsed"

COMMAND_PAGES: list[tuple[list[str], str]] = [
    (["device", "status"], "home"),
    (["device", "device-list"], "devices"),
    (["device", "system-information"], "sysinfo"),
    (["device", "access-code"], "routerpasswd"),
    (["device", "remote-access"], "remoteaccess"),
    (["device", "restart-device"], "restart"),
    (["broadband", "status"], "broadbandstatistics"),
    (["broadband", "configure"], "broadbandconfig"),
    (["broadband", "fiber-status"], "fiberstat"),
    (["home-network", "status"], "lanstatistics"),
    (["home-network", "configure"], "etherlan"),
    (["home-network", "ipv6"], "ip6lan"),
    (["home-network", "wi-fi"], "wconfig_unified"),
    (["home-network", "advanced-wi-fi"], "wconfig"),
    (["home-network", "mac-filtering"], "wmacauth"),
    (["home-network", "subnets-dhcp"], "dhcpserver"),
    (["home-network", "ip-allocation"], "ipalloc"),
    (["voice", "status"], "voice"),
    (["voice", "line-details"], "voiceconfig"),
    (["voice", "call-statistics"], "voicestat"),
    (["firewall", "status"], "firewall"),
    (["firewall", "packet-filter"], "packetfilter"),
    (["firewall", "nat-gaming"], "apphosting"),
    (["firewall", "public-subnet-hosts"], "pshosts"),
    (["firewall", "ip-passthrough"], "ippass"),
    (["firewall", "firewall-advanced"], "dosprotect"),
    (["firewall", "security-options"], "securityoptions"),
    (["diagnostics", "troubleshoot"], "diag"),
    (["diagnostics", "speed-test"], "speed"),
    (["diagnostics", "logs"], "logs"),
    (["diagnostics", "update"], "update"),
    (["diagnostics", "resets"], "reset"),
    (["diagnostics", "syslog"], "syslog"),
    (["diagnostics", "event-notifications"], "events"),
    (["diagnostics", "nat-table"], "nattable"),
]


def read_expected(page: str) -> dict:
    return json.loads((EXPECTED_DIR / f"{page}.json").read_text())


def read_parsed(page: str) -> dict:
    return json.loads((PARSED_DIR / f"{page}.json").read_text())


def _snake(name: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in name)


def _rows(cls, items: list[dict] | None, nested: dict[str, type] | None = None) -> list:
    out = []
    for item in items or []:
        kwargs = {_snake(k): v for k, v in item.items()}
        for key, sub in (nested or {}).items():
            if kwargs.get(key) is not None:
                kwargs[key] = _rows(sub, kwargs[key])
        out.append(cls(**kwargs))
    return out


def parsed_page_from_json(data: dict) -> ParsedPage:
    """Rebuild a ParsedPage from the camelCase parsed/<page>.json fixture (inverse of to_json_dict)."""
    return ParsedPage(
        page=data["page"],
        title=data.get("title", ""),
        heading=data.get("heading", ""),
        values=dict(data.get("values", {})),
        tables=list(data.get("tables", [])),
        fields=_rows(ParsedField, data.get("fields")),
        selects=_rows(ParsedSelect, data.get("selects"), {"option_details": SelectOption}),
        textareas=_rows(ParsedTextarea, data.get("textareas")),
        buttons=_rows(ParsedButton, data.get("buttons")),
        forms=_rows(ParsedForm, data.get("forms")),
        value_entries=_rows(ParsedValueEntry, data["valueEntries"]) if "valueEntries" in data else None,
        links=_rows(ParsedLink, data["links"]) if "links" in data else None,
    )


def require_fixture(path: Path) -> None:
    if not path.exists():
        pytest.skip(
            f"{path.relative_to(FIXTURE_ROOT.parent)} not present; capture the pack with `bgwcli fixtures-capture`"
        )


@pytest.mark.parametrize(("command", "page"), COMMAND_PAGES, ids=[" ".join(c) for c, _ in COMMAND_PAGES])
def test_human_router_tab_commands_resolve_to_captured_fixture_pages(command: list[str], page: str):
    pages = pytest.importorskip("bgwcli.pages")
    root, *args = command
    tab = pages.resolve_section_command(root, args)
    assert tab is not None, " ".join(command)
    assert tab.page == page
    expected_path = EXPECTED_DIR / f"{page}.json"
    pack_present = EXPECTED_DIR.is_dir() and any(EXPECTED_DIR.glob("*.json"))
    if pack_present:
        # A captured pack must cover every command page; a missing one is a gap, not a pass.
        assert expected_path.exists(), f"{expected_path.name} missing from the captured fixture pack"
        expected = read_expected(page)
        assert expected["page"] == page
        assert expected["secretsRedacted"] is True


def test_parsed_page_from_json_round_trips_to_json_dict():
    from bgwcli.types import to_json_dict

    page = ParsedPage(
        page="diag",
        title="Diagnostics",
        heading="Troubleshoot",
        values={"Description": "x"},
        tables=[{"Test": "DNS", "Result": "Pass"}],
        fields=[ParsedField("protopref", "radio", "IPv4", True, False, disabled=False)],
        selects=[ParsedSelect("mode", "a", ["a", "b"], False, option_details=[SelectOption("a", "A", True, False)])],
        textareas=[ParsedTextarea("ProgressWindow", "", False, read_only=True)],
        buttons=[ParsedButton("Ping", "submit", "Ping", "Ping", False)],
        forms=[ParsedForm("post", "/cgi-bin/diag.ha", ["protopref"], ["mode"], ["ProgressWindow"], ["Ping"])],
        value_entries=[ParsedValueEntry("", "Description", "x", default_value=None)],
        links=[ParsedLink("Home", "/cgi-bin/home.ha", "router-page", page="home")],
    )
    as_json = json.loads(json.dumps(to_json_dict(page)))
    assert parsed_page_from_json(as_json) == page
    assert parsed_page_from_json({"page": "x"}).value_entries is None


def test_diagnostics_troubleshoot_fixture_proves_inputs_actions_progress_and_form_target():
    require_fixture(EXPECTED_DIR / "diag.json")
    expected = read_expected("diag")

    for flag in (
        "pageLoads", "dataObtainable", "usefulFieldsExist", "usefulTablesExist",
        "buttonsDiscovered", "formsDiscovered", "notOnlyJunk",
    ):
        assert expected[flag] is True, flag
    assert "WebAddress" in expected["fieldNames"]
    assert "protopref" in expected["fieldNames"]
    # Current firmware renders the progress window without a textarea; a capture holding one is just as valid.
    assert set(expected["textareaNames"]) <= {"ProgressWindow"}
    assert set(expected["buttonNames"]) >= {
        "AuthDetails", "DNSDetails", "EthDetails", "IPDetails", "Lookup", "Ping",
        "RunFullDiagnostics", "SendDiagnostics", "Trace",
    }
    assert expected["formActions"] == ["/cgi-bin/diag.ha"]


def test_fixture_backed_parsed_page_json_includes_scriptable_summaries():
    require_fixture(PARSED_DIR / "diag.json")
    fmt = pytest.importorskip("bgwcli.format")

    def parsed_output(page: str) -> dict:
        return fmt.parsed_page_output(parsed_page_from_json(read_parsed(page)))

    # Counts come from the captured rows: a fresh capture has a different speed-test history and
    # NAT session count, and the summaries must agree with whatever the pack holds.
    # A freshly reset gateway has no speed-test history at all; the summary then has no result counts.
    speed_rows = read_parsed("speed")["tables"]
    speed = parsed_output("speed")
    assert len(speed["tables"]) == len(speed_rows)
    if speed_rows:
        assert speed["summary"]["Results"] == str(len(speed_rows))
        for result, count in Counter(row.get("Result") or "(blank)" for row in speed_rows).items():
            assert f"{result}: {count}" in speed["summary"]["By result"].split(", ")
    else:
        assert "Results" not in speed["summary"]

    nat_rows = read_parsed("nattable")["tables"]
    nat_table = parsed_output("nattable")
    assert isinstance(nat_table["summary"]["Total sessions available"], str)
    assert nat_table["summary"].get("Displayed sessions") == (str(len(nat_rows)) if nat_rows else None)

    diagnostics = parsed_output("diag")
    assert isinstance(diagnostics["summary"]["Description"], str)
    assert diagnostics["summary"]["Field protopref"] == "IPv4"
    tests = {row.get("Test") for row in diagnostics["tables"]}
    assert tests >= {"Ethernet", "Authentication", "IP", "DNS"}


def test_a_speed_page_without_history_has_no_results_to_summarise():
    from bgwcli import format as fmt
    from bgwcli.parser import parse_page

    html = "<html><h1>Speed Test</h1><form><input type=submit name=Start value=Start></form></html>"
    page = parse_page("speed", html)
    output = fmt.parsed_page_output(page)
    assert output["tables"] == [] and "Results" not in output["summary"]
