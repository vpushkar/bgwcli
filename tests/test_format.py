"""Ported from tests/format.test.ts plus the printer expectations embedded in cli.ts / command-fixtures.test.ts.

Every printer takes a `stream` argument; tests capture through io.StringIO."""

from __future__ import annotations

import io
import json

from bgwcli import format as fmt
from bgwcli.actions import ROUTER_ACTIONS
from bgwcli.audit import build_audit
from bgwcli.devices import DeviceListResult
from bgwcli.fetch import ParsedPageResult
from bgwcli.mutations import MutationPlan
from bgwcli.operations import (
    OperationResult,
    action_committed,
    action_dry_run,
    diagnostic_committed,
    restore_committed,
    set_committed,
    set_dry_run,
    submit_committed,
)
from bgwcli.pages import ROUTER_TABS
from bgwcli.parser import parse_page
from bgwcli.restore import (
    RestoreDeferredForward,
    RestoreExecution,
    RestoreFollowUp,
    RestoreStep,
    RestoreStepResult,
)
from bgwcli.session import SessionState
from bgwcli.snapshot import Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService
from bgwcli.snapshot_diff import EntryDiff, FormFieldDiff, ReservationChange, ReservationDiff, SnapshotDiff
from bgwcli.status import StatusResult, StatusSection
from bgwcli.sweep import SweepPage
from bgwcli.types import Device, LogEntry, ParsedButton, ParsedPage, SitemapEntry

ESC = "\x1b"


def capture(fn, *args, **kwargs) -> str:
    stream = io.StringIO()
    fn(*args, stream=stream, **kwargs)
    return stream.getvalue()


def empty_diff(**overrides) -> SnapshotDiff:
    base = dict(
        identical=True,
        services=EntryDiff(),
        forwards=EntryDiff(),
        reservations=ReservationDiff(),
        forms={},
        firmware_changed=False,
    )
    base.update(overrides)
    return SnapshotDiff(**base)


# --- sanitizing -------------------------------------------------------------------------------


def test_sanitize_terminal_text_strips_ansi_osc_and_control_chars():
    value = f"{ESC}[31mred{ESC}[0m {ESC}]52;c;payload\x07name\x00\x7f\x9b tab\tnl\n"
    assert fmt.sanitize_terminal_text(value) == "red name tab\tnl\n"
    assert fmt.sanitize_terminal_text(value, single_line=True) == "red name tab nl "


def test_human_output_strips_terminal_control_sequences():
    output = capture(fmt.print_key_values, {
        "Status": f"{ESC}[31mred{ESC}[0m",
        "Device": f"{ESC}]52;c;payload\x07name",
    })
    assert "red" in output
    assert "name" in output
    assert ESC not in output
    assert "payload" not in output


def test_print_key_values_pads_to_widest_key_capped_at_42():
    output = capture(fmt.print_key_values, {"A": "1", "Longer key": "2"})
    assert output == "A".ljust(12) + "  1\n" + "Longer key".ljust(12) + "  2\n"
    wide = capture(fmt.print_key_values, {"x" * 60: "v"})
    assert wide == "x" * 60 + "  v\n"


# --- generic table ----------------------------------------------------------------------------


def test_render_rows_truncates_wide_cells_and_reports_none():
    rows = [{"Name": "a" * 50, "Value": "1"}]
    output = capture(fmt.print_rows, rows, ["Name", "Value"])
    lines = output.splitlines()
    assert lines[0] == "Name" + " " * 32 + "  Value"
    assert lines[1] == "-" * 36 + "  " + "-" * 5
    assert lines[2].startswith("a" * 35 + "…")
    assert capture(fmt.print_rows, [], ["Name"]) == "(none)\n"


# --- parsed page printers (from format.test.ts) ----------------------------------------------


def test_wifi_summary_uses_form_state_instead_of_noisy_option_text():
    page = parse_page("wconfig_unified", """
    <title>Wi-Fi</title>
    <table><tr><td>Home SSID Enable Default: On</td><td>Off On</td></tr></table>
    <form method="post" action="/cgi-bin/wconfig_unified.ha">
      <input name="nonce" value="abc">
      <input name="home_ssidname" value="HomeNet">
      <input name="homeSSID_key" value="secret">
      <input name="guest_ssidname" value="GuestNet">
      <input name="u_octet" value="2">
      <select name="u_ussidenable"><option value="off">Off</option><option value="on" selected>On</option></select>
      <select name="homeSSID_security">
        <option value="wpa">WPA</option><option value="defwpa" selected>Default</option>
      </select>
      <select name="u_gssidenable"><option value="off" selected>Off</option><option value="on">On</option></select>
      <select name="u_gssidisolate">
        <option value="on" selected>Internet Only</option><option value="off">LAN</option>
      </select>
      <input type="submit" name="Save" value="Save">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Home SSID" in output
    assert "HomeNet" in output
    assert "Home password" in output
    assert "[redacted]" in output
    assert "Guest access" in output
    assert "Internet Only" in output
    assert "Guest subnet" in output
    assert "192.168.2.0/24" in output
    assert "Off On" not in output


def test_nat_gaming_summary_does_not_dump_every_device_option():
    page = parse_page("apphosting", """
    <title>NAT/Gaming</title>
    <table><tr><td>Needed by Device</td><td>device-a device-b device-c</td></tr></table>
    <form method="post" action="/cgi-bin/apphosting.ha">
      <input name="nonce" value="abc">
      <select name="service"><option value="HTTP" selected>HTTP</option><option value="SSH">SSH</option></select>
      <select name="device"><option value="aa:bb" selected>laptop</option><option value="cc:dd">server</option></select>
      <input type="submit" name="Add" value="Add">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Selected service" in output
    assert "HTTP" in output
    assert "Known devices" in output
    assert "device-a device-b device-c" not in output


def test_access_code_summary_shows_redacted_form_state_by_default():
    page = parse_page("routerpasswd", """
    <title>Access Code</title>
    <form method="post" action="/cgi-bin/routerpasswd.ha">
      <input name="nonce" value="abc">
      <input name="old_password" value="old-secret">
      <input name="new_password" value="new-secret">
      <input name="confirm_password" value="new-secret">
      <input type="submit" name="Save" value="Save">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Old Password" in output
    assert "[redacted]" in output
    assert "old-secret" not in output
    assert "new-secret" not in output
    assert "Available actions" in output


def test_restart_style_pages_surface_actions_without_form_noise():
    page = parse_page("restart", """
    <title>Restart Device</title>
    <form method="post" action="/cgi-bin/restart.ha">
      <input name="nonce" value="abc">
      <input type="submit" name="Restart" value="Restart Device">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Available actions" in output
    assert "Restart Device" in output
    assert "Controls" in output
    assert "Fields        0" in output


def test_wifi_human_output_includes_each_router_reported_radio_channel():
    page = parse_page("wconfig_unified", """
    <title>Wi-Fi</title>
    <h2>2.4 GHz Wi-Fi Channel Selection</h2>
    <table><tr><th>Current Channel</th><td>6 (20 MHz)</td><td>Automatic</td></tr></table>
    <h2>5 GHz Wi-Fi Channel Selection</h2>
    <table><tr><th>Current Channel</th><td>60 (80 MHz),161 (80 MHz)</td><td>Automatic</td></tr></table>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Current radios" in output
    assert "2.4 GHz" in output
    assert "5 GHz low-band" in output
    assert "5 GHz high-band" in output


def test_voice_output_omits_a_meaningless_blank_section_summary():
    page = parse_page("voice", """
    <title>Voice Status</title>
    <table>
      <tr><th>Metric</th><th>Line 1</th><th>Line 2</th></tr>
      <tr><td>Line Status</td><td>Registered</td><td>Disabled</td></tr>
    </table>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert "Reported rows" in output
    assert "Sections" not in output
    assert "(blank)" not in output


def test_generic_page_prints_values_tables_actions_controls_and_forms():
    page = parse_page("securityoptions", """
    <title>Security Options</title>
    <table><tr><td>Firewall</td><td>On</td></tr></table>
    <form method="post" action="/cgi-bin/securityoptions.ha">
      <input name="nonce" value="abc">
      <input name="mode" value="strict">
      <select name="level"><option value="1" selected>Low</option><option value="2">High</option></select>
      <textarea name="notes">n</textarea>
      <input type="submit" name="Save" value="Save">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page)
    assert output.startswith("Security Options\n\n")
    assert "Firewall" in output and "On" in output
    assert "Available actions" in output
    assert "Controls" in output
    assert "Use --forms" in output

    forms_output = capture(fmt.print_parsed_page, page, forms=True)
    assert "CLI operations" in forms_output
    assert "submit securityoptions Save" in forms_output
    assert "submit securityoptions Save --commi…" in forms_output  # cell clipped at 36 chars like TS
    assert "\nFields\n" in forms_output
    assert "\nSelects\n" in forms_output
    assert "Low=1, High=2" in forms_output
    assert "\nTextareas\n" in forms_output
    assert "\nButtons\n" in forms_output
    assert "\nForms\n" in forms_output
    assert "/cgi-bin/securityoptions.ha" in forms_output
    assert "Use --forms" not in forms_output


def test_forms_view_blocks_commit_command_on_dangerous_pages():
    page = parse_page("reset", """
    <title>Resets</title>
    <form method="post" action="/cgi-bin/reset.ha">
      <input type="submit" name="Reset" value="Reset Device">
    </form>
    """)
    output = capture(fmt.print_parsed_page, page, forms=True)
    assert "blocked on dangerous page" in output


def test_forms_view_commit_command_carries_page_token_and_quotes_odd_button_names():
    page = ParsedPage(page="x", title="", heading="", buttons=[
        ParsedButton("S", "submit", "S", "S", False), ParsedButton("Go now", "submit", "Go", "Go", False),
    ])
    output = capture(fmt.print_parsed_page, page, forms=True)
    assert "submit x S --commit --confirm X" in output
    assert 'submit x "Go now"' in output


def test_table_overflow_uses_limit_hint():
    page = ParsedPage(
        page="sysinfo",
        title="System Information",
        heading="",
        values={},
        tables=[{"Col": f"row{i}"} for i in range(25)],
    )
    output = capture(fmt.print_parsed_page, page, limit=5)
    assert "... 20 more rows. Use --limit 25 to show all." in output
    assert "row4" in output
    assert "row5" not in output


def test_ipalloc_and_nattable_and_speed_summaries():
    ipalloc = ParsedPage(
        page="ipalloc", title="IP Allocation", heading="", values={},
        tables=[
            {"IPv4 Address / Name": "192.168.1.2 / a", "MAC Address": "aa", "Status": "on", "Allocation": "DHCP"},
            {"IPv4 Address / Name": "192.168.1.3 / b", "MAC Address": "bb", "Status": "off", "Allocation": "Fixed"},
            {"IPv4 Address / Name": "192.168.1.4 / c", "MAC Address": "cc", "Status": "on", "Allocation": "DHCP"},
        ],
    )
    summary = fmt.summarize_parsed_page(ipalloc)
    assert summary == {"Total entries": "3", "By allocation": "DHCP: 2, Fixed: 1", "By status": "on: 2, off: 1"}
    output = capture(fmt.print_parsed_page, ipalloc)
    assert "Allocations" in output and "192.168.1.4 / c" in output

    nattable = ParsedPage(
        page="nattable", title="NAT Table", heading="",
        values={"Total sessions available": "8192", "Total sessions in use": "2"},
        tables=[{"Protocol": "tcp"}, {"Protocol": "udp"}, {"Protocol": "tcp"}],
    )
    assert fmt.summarize_parsed_page(nattable) == {
        "Total sessions available": "8192",
        "Total sessions in use": "2",
        "Displayed sessions": "3",
        "By protocol": "tcp: 2, udp: 1",
    }
    assert "Sessions" in capture(fmt.print_parsed_page, nattable)

    speed = ParsedPage(
        page="speed", title="Speed Test", heading="", values={"x": "y"},
        tables=[
            {"Time": "t1", "Direction": "Downstream", "Mbps": "900", "Latency ms": "3", "Result": "Success"},
            {"Time": "t1", "Direction": "Upstream", "Mbps": "800", "Latency ms": "3", "Result": "Success"},
        ],
    )
    assert fmt.summarize_parsed_page(speed) == {
        "Results": "2",
        "By result": "Success: 2",
        "Latest downstream Mbps": "900",
        "Latest upstream Mbps": "800",
    }
    assert "History" in capture(fmt.print_parsed_page, speed)
    assert fmt.summarize_parsed_page(ParsedPage(page="speed", title="", heading="", values={"x": "y"})) == {"x": "y"}


def test_parsed_page_output_has_camel_case_keys_and_summary():
    page = parse_page("dhcpserver", """
    <title>Subnets &amp; DHCP</title>
    <form method="post" action="/cgi-bin/dhcpserver.ha">
      <input name="ipaddr" value="192.168.1.254">
      <input name="ipmask" value="255.255.255.0">
      <select name="dhcp"><option value="on" selected>On</option><option value="off">Off</option></select>
      <input name="dhcpstart" value="192.168.1.64">
      <input name="dhcpend" value="192.168.1.253">
      <input name="dhcpday" value="1"><input name="dhcphour" value="0">
      <input name="dhcpmin" value="0"><input name="dhcpsec" value="0">
      <input type="radio" name="primpool" value="private" checked>
      <select name="pubsub"><option value="off" selected>Off</option></select>
    </form>
    """)
    out = fmt.parsed_page_output(page)
    assert out["page"] == "dhcpserver"
    assert "valueEntries" in out and "value_entries" not in out
    assert out["summary"]["Gateway address"] == "192.168.1.254"
    assert out["summary"]["DHCP enabled"] == "On"
    assert out["summary"]["DHCP lease"] == "1d 0h 0m 0s"
    assert out["summary"]["Primary pool"] == "private"
    assert out["summary"]["Public subnet"] == "Off"
    json.dumps(out)  # must be serializable


def test_form_state_values_orders_preferred_first_then_alpha_and_labelizes():
    page = parse_page("etherlan", """
    <form method="post" action="/cgi-bin/etherlan.ha">
      <input name="nonce" value="n">
      <input type="hidden" name="hid" value="1">
      <input name="zeta" value="z">
      <input name="ipmask" value="255.255.255.0">
      <input name="ipaddr" value="192.168.1.254">
      <input type="checkbox" name="u_someFlag" checked>
      <select name="lanipv6"><option value="on" selected>On</option></select>
    </form>
    """)
    values = fmt.summarize_parsed_page(page)
    assert list(values) == ["Lanipv6", "Ipaddr", "Ipmask", "Some Flag", "Zeta"]
    assert values["Lanipv6"] == "On"
    assert values["Some Flag"] == "Yes"
    assert "Hid" not in values and "Nonce" not in values


# --- devices / logs ---------------------------------------------------------------------------


def make_device(i: int, status: str = "on") -> Device:
    return Device(
        status=status,
        name=f"dev{i}",
        ip=f"192.168.1.{i}",
        mac=f"aa:bb:cc:dd:ee:{i:02x}",
        connection="Wi-Fi 5 GHz Type: Wireless",
        connection_speed="866",
        last_activity="now",
    )


def test_print_device_list_summarizes_and_limits():
    devices = [make_device(i) for i in range(1, 6)] + [make_device(9, status="off")]
    result = DeviceListResult(fallback=False, devices=devices)
    output = capture(fmt.print_device_list, result, limit=3)
    assert "Total devices  6" in output
    assert "By status" in output and "on: 5, off: 1" in output
    assert "By connection" in output and "Wi-Fi 5 GHz: 6" in output
    assert "Type: Wireless" not in output
    assert "dev3" in output and "dev4" not in output
    assert "... 3 more rows. Use --limit 6 to show all." in output


def test_print_device_list_fallback_header_and_empty():
    result = DeviceListResult(fallback=True, devices=[], error="boom")
    output = capture(fmt.print_device_list, result)
    assert output.startswith("Device List (fallback from IP Allocation)\ndevices.ha did not return: boom\n\n")
    assert "(none)" in output


def test_print_logs_empty_and_populated():
    assert capture(fmt.print_logs, []) == "No log entries found.\n"
    logs = [
        LogEntry("1", "t1", "s", "d", "TCP", "blocked"),
        LogEntry("2", "t2", "s", "d", "UDP", "blocked"),
        LogEntry("3", "t3", "s", "d", "", "allowed"),
    ]
    output = capture(fmt.print_logs, logs)
    assert "Entries".ljust(12) + "  3" in output
    assert "blocked: 2, allowed: 1" in output
    assert "TCP: 1, UDP: 1, (blank): 1" in output
    assert "Destination" in output and "t3" in output


# --- sweep / scan / audit ---------------------------------------------------------------------


def make_scan(page: str, ok: bool = True, **kw) -> SweepPage:
    base = dict(section="device", label=page, page=page, dangerous=False, guarded=False, ok=ok)
    base.update(kw)
    return SweepPage(**base)


def test_print_scans_columns():
    scans = [
        make_scan("home", status_code=200, title="Home", value_count=3, data_count=3),
        make_scan("bad", ok=False, error="timeout", dangerous=True, fallback=True),
    ]
    output = capture(fmt.print_scans, scans)
    header = output.splitlines()[0]
    for column in ("Section", "Tab", "Page", "Status", "Fallback", "Title", "Values", "Data", "Guarded"):
        assert column in header
    assert "200" in output and "error" in output and "timeout" in output and "yes" in output


def test_print_audit_summary_and_needs_attention():
    scans = [
        make_scan("home", status_code=200, title="Home", data_count=3),
        make_scan("empty", status_code=200, title="Empty", data_count=0),
        make_scan("bad", ok=False, error="timeout"),
    ]
    audit = build_audit(scans)
    output = capture(fmt.print_audit, audit)
    assert "Total pages     3" in output
    assert "OK pages        2" in output
    assert "Failed pages    1" in output
    assert "Useful pages    1" in output
    assert "Empty OK pages  1" in output
    assert "Needs attention" in output
    assert "empty" in output and "bad" in output
    assert "\nhome" not in output.split("Needs attention")[1]

    clean = build_audit([make_scan("home", status_code=200, data_count=3)])
    assert "Needs attention" not in capture(fmt.print_audit, clean)


def test_print_page_fetch_error():
    result = ParsedPageResult(page="diag", ok=False, error=f"{ESC}[1mnope{ESC}[0m")
    assert capture(fmt.print_page_fetch_error, result) == "Page unavailable: diag\nnope\n"
    assert capture(fmt.print_page_fetch_error, ParsedPageResult(page="x", ok=False)) == "Page unavailable: x\n"


# --- operations / mutation plans --------------------------------------------------------------


def test_print_operation_dry_run_and_committed():
    action = next(a for a in ROUTER_ACTIONS if a.name == "restart")
    dry = action_dry_run(action, dict(action.payload))
    output = capture(fmt.print_operation, dry)
    assert output.startswith("dry-run: no router action was sent\n")
    assert "Guarded         yes" in output
    assert "Dangerous       yes" in output
    assert "Confirmation    RESTART" in output
    assert "Commit command  action restart --commit --confirm RESTART" in output
    assert 'Payload         {"Restart":"Restart Device"}' in output

    committed = action_committed(action, 302, "/cgi-bin/home.ha")
    output = capture(fmt.print_operation, committed)
    assert output.startswith("action committed\n")
    assert "Status".ljust(12) + "  302" in output
    assert "Location".ljust(12) + "  /cgi-bin/home.ha" in output

    with_result = OperationResult(
        operation="diagnostic", dry_run=False, committed=True, page="diag", guarded=True, dangerous=False,
        target="example.com", result="PING ok",
    )
    output = capture(fmt.print_operation, with_result)
    assert "diagnostic committed" in output
    assert "Target".ljust(12) + "  example.com" in output
    assert output.endswith("\nResult\nPING ok\n")


def test_print_mutation_plan_shows_payload_and_commit_command():
    plan = MutationPlan(
        page="dosprotect",
        blocked=False,
        raw_payload={"nonce": "n", "algsip": "on"},
        display_payload={"nonce": "[redacted]", "algsip": "on"},
        display_changes={"algsip": "on"},
    )
    output = capture(fmt.print_mutation_plan, plan, confirmation="DOSPROTECT")
    assert output.startswith("dry-run: no router set was sent\n")
    assert '{"nonce":"[redacted]","algsip":"on"}' in output
    assert '{"algsip":"on"}' in output
    assert "--commit --confirm DOSPROTECT" in output
    # identical to printing the set_dry_run operation
    assert output == capture(fmt.print_operation, set_dry_run(plan, "DOSPROTECT"))


def test_operation_output_emits_explicit_null_location_when_committed():
    action = next(a for a in ROUTER_ACTIONS if a.name == "restart")
    committed = fmt.operation_output(action_committed(action, 200, None))
    assert "location" in committed and committed["location"] is None
    assert committed["statusCode"] == 200
    assert "commitCommand" not in committed

    dry = fmt.operation_output(action_dry_run(action, {}))
    assert "location" not in dry
    assert "statusCode" not in dry


def test_json_with_nulls_keeps_selected_none_fields_recursively():
    execution = RestoreExecution(steps=[
        RestoreStepResult(order=1, page="services", kind="add-service", description="d", status="applied",
                          status_code=302, location=None),
        RestoreStepResult(order=2, page="apphosting", kind="add-forward", description="d", status="blocked"),
    ])
    out = fmt.json_with_nulls(execution, null_keys={"location"})
    assert out["steps"][0] == {
        "order": 1, "page": "services", "kind": "add-service", "description": "d",
        "status": "applied", "statusCode": 302, "location": None,
    }
    assert "location" in out["steps"][1] and out["steps"][1]["location"] is None
    assert "stoppedAt" not in out


def test_operation_output_null_location_only_for_action_set_submit():
    # TS actionCommitted/setCommitted/submitCommitted take `location: string | null` and always set it;
    # diagnosticCommitted / restoreCommitted never touch it, so JSON.stringify drops the key.
    assert fmt.operation_output(set_committed("dosprotect", 302, None))["location"] is None
    assert fmt.operation_output(submit_committed("dosprotect", "Save", 200, None))["location"] is None
    assert fmt.operation_output(set_committed("dosprotect", 302, "/cgi-bin/x.ha"))["location"] == "/cgi-bin/x.ha"
    diag = fmt.operation_output(diagnostic_committed("ping", "8.8.8.8", 200, "ok"))
    assert diag["statusCode"] == 200 and "location" not in diag
    execution = RestoreExecution(steps=[
        RestoreStepResult(order=1, page="services", kind="add-service", description="d", status="applied",
                          status_code=302, location=None),
    ])
    restored = fmt.operation_output(restore_committed(execution))
    assert restored["committed"] is True and "location" not in restored


def test_execution_output_emits_location_only_for_posted_steps():
    """Mirror restore.ts executeRestore: `location` is set only on result objects built from a POST
    response (applied / failed with statusCode) and on confirmWarningPage failures (string, never null)."""
    execution = RestoreExecution(steps=[
        RestoreStepResult(order=1, page="services", kind="add-service", description="d", status="applied",
                          status_code=302, location=None),
        RestoreStepResult(order=2, page="services", kind="add-service", description="d", status="failed",
                          status_code=500, location=None, error="unexpected HTTP 500"),
        RestoreStepResult(order=3, page="apphosting", kind="add-forward", description="d", status="failed",
                          error="connection reset"),  # thrown before any response: TS has no `location`
        RestoreStepResult(order=4, page="apphosting", kind="add-forward", description="d", status="blocked",
                          error="no payload"),
        RestoreStepResult(order=5, page="ipalloc", kind="skip", description="d", status="skipped"),
        RestoreStepResult(order=6, page="ipalloc", kind="reserve", description="d", status="not-run"),
        RestoreStepResult(order=7, page="wconfig", kind="set-form", description="d", status="failed",
                          location="/cgi-bin/wconfigwarn.ha", error="cannot derive confirmation page"),
    ], stopped_at=2)
    steps = fmt.execution_output(execution)["steps"]
    assert steps[0]["location"] is None and steps[0]["statusCode"] == 302
    assert steps[1]["location"] is None and steps[1]["statusCode"] == 500
    for i in (2, 3, 4, 5):
        assert "location" not in steps[i], steps[i]
    assert steps[6]["location"] == "/cgi-bin/wconfigwarn.ha" and "statusCode" not in steps[6]
    # key order matches TS: statusCode, location, error
    assert list(steps[1]) == ["order", "page", "kind", "description", "status", "statusCode", "location", "error"]


def test_print_json_serializes_dataclasses_with_indent(capsys):
    state = SessionState(cached=False)
    fmt.print_json(state)
    assert capsys.readouterr().out == '{\n  "cached": false\n}\n'
    stream = io.StringIO()
    fmt.print_json({"a": [1, None]}, stream=stream)
    assert stream.getvalue() == '{\n  "a": [\n    1,\n    null\n  ]\n}\n'


# --- status views -----------------------------------------------------------------------------


def test_print_status_sections_for_status_command():
    sections = [
        StatusSection(page="sysinfo", ok=True, title="System Information", values={"Model": "BGW320"}),
        StatusSection(page="broadbandstatistics", ok=False, error="timeout"),
        StatusSection(page="firewall", ok=True, values={"Firewall": "On"}),
    ]
    output = capture(fmt.print_status_sections, sections)
    assert output == (
        "\nSystem Information\n" + "Model".ljust(12) + "  BGW320\n\nbroadbandstatistics\ntimeout\n\nfirewall\n"
        + "Firewall".ljust(12) + "  On\n"
    )


def test_print_device_status_prefers_parsed_page_and_falls_back_to_summary():
    parsed = ParsedPage(page="home", title="Device Status", heading="", values={"Model": "BGW320"})
    output = capture(fmt.print_device_status, StatusResult(page="home", fallback=False, parsed=parsed))
    assert output.startswith("Device Status\n\n")
    assert "Model".ljust(12) + "  BGW320" in output

    sections = [
        StatusSection(page="sysinfo", ok=True, title="System Information",
                      values={"Manufacturer": "Nokia", "Model Number": "BGW320-505", "Noise": "x"}),
        StatusSection(page="broadbandstatistics", ok=False, error="timeout"),
    ]
    fallback = StatusResult(page="home", fallback=True, error="home.ha 500", sections=sections)
    output = capture(fmt.print_device_status, fallback)
    assert output.startswith("Device Status (fallback summary)\n\nhome.ha did not return: home.ha 500\n\n")
    assert "Manufacturer" in output and "Nokia" in output
    assert "Noise" not in output
    assert "broadbandstatistics\ntimeout\n" in output


def test_print_composite_status_fallback_prints_sections_with_tables():
    sections = [
        StatusSection(page="ipalloc", ok=True, heading="IP Allocation", values={"Count": "2"},
                      tables=[{"IP": "1"}, {"IP": "2"}, {"IP": "3"}]),
    ]
    result = StatusResult(page="lanstatistics", fallback=True, error="500", sections=sections)
    output = capture(fmt.print_composite_status, "Home Network Status", result, limit=2)
    assert output.startswith("Home Network Status (fallback)\n\nlanstatistics.ha did not return: 500\n\n")
    assert (
        "IP Allocation\n" + "Count".ljust(12) + "  2\n\nIP\n--\n1 \n2 \n... 1 more rows. Use --limit 3 to show all.\n"
    ) in output

    parsed = ParsedPage(page="lanstatistics", title="LAN", heading="", values={"a": "b"})
    direct = capture(fmt.print_composite_status, "Home Network Status", StatusResult(
        page="lanstatistics", fallback=False, parsed=parsed))
    assert direct.startswith("LAN\n\n")


# --- listings: sitemap / tabs / actions / session / coverage ----------------------------------


def test_print_sitemap_pads_page_column():
    entries = [SitemapEntry("home", "Device Status", "/cgi-bin/home.ha"), SitemapEntry("wconfig", "Wi-Fi", "/x")]
    assert capture(fmt.print_sitemap, entries) == "home     Device Status\nwconfig  Wi-Fi\n"


def test_print_tabs_lists_section_label_page_and_guard():
    output = capture(fmt.print_tabs, ROUTER_TABS)
    lines = output.splitlines()
    assert len(lines) == len(ROUTER_TABS)
    restart = next(line for line in lines if line.rstrip().endswith("restart  guarded"))
    assert restart.startswith("Device")
    assert any(line.split()[-1] == "home" for line in lines)


def test_print_actions_lists_every_router_action_with_token():
    output = capture(fmt.print_actions, ROUTER_ACTIONS)
    lines = output.splitlines()
    assert len(lines) == len(ROUTER_ACTIONS)
    restart = next(line for line in lines if line.startswith("restart "))
    assert "dangerous" in restart
    assert "confirm=RESTART" in restart
    assert "Restart the gateway." in restart
    assert any(" guarded " in line for line in lines)


def test_print_session_state_formats_iso_timestamps():
    assert capture(fmt.print_session_state, SessionState(cached=False)) == (
        "Cached session".ljust(19) + "  no\n" + "Cache expires".ljust(19) + "  (none)\n"
        + "Pool cooldown until  (none)\n"
    )
    output = capture(fmt.print_session_state, SessionState(
        cached=True, cache_expires_at=1_700_000_000_123, pool_cooldown_until=1_700_000_500_000))
    assert "Cached session       yes" in output
    assert "2023-11-14T22:13:20.123Z" in output
    assert "2023-11-14T22:21:40.000Z" in output


def test_coverage_output_and_printer():
    coverage = fmt.coverage_output(mapped_pages=["a", "b", "b"], live_pages=["b", "c"])
    assert coverage == {"mappedCount": 2, "liveCount": 2, "missingFromCli": ["c"], "notInLiveSitemap": ["a"]}
    output = capture(fmt.print_coverage, coverage)
    assert "Mapped pages         2" in output
    assert "Live sitemap pages   2" in output
    assert "Missing from CLI     c" in output
    assert "Not in live sitemap  a" in output
    none = capture(fmt.print_coverage, fmt.coverage_output(mapped_pages=["a"], live_pages=["a"]))
    assert none.count("(none)") == 2


# --- snapshot summary / diff (from format.test.ts) --------------------------------------------


def test_print_snapshot_summary_prints_counts_and_path():
    snapshot = Snapshot(
        meta=SnapshotMeta(firmware="4.27.7", ts="t", router_host="r"),
        services=[SnapshotService("a", 1, 1, 1, "TCP")],
        forms={"dosprotect": {}},
        tables={"packetfilter": []},
    )
    output = capture(fmt.print_snapshot_summary, snapshot, "/x/dump.json")
    assert output.startswith("Dump written: /x/dump.json\n")
    assert "Firmware: 4.27.7\n" in output
    assert "Services: 1\n" in output
    assert "Forwards: 0\n" in output
    assert "Reservations: 0\n" in output
    assert "Forms: dosprotect\n" in output
    assert "Tables: packetfilter\n" in output
    assert fmt.snapshot_summary_output(snapshot, "/x/dump.json") == {
        "path": "/x/dump.json",
        "meta": {"firmware": "4.27.7", "ts": "t", "routerHost": "r", "schema": 2},
        "services": 1,
        "forwards": 0,
        "reservations": 0,
        "forms": ["dosprotect"],
        "tables": ["packetfilter"],
    }


def test_print_snapshot_diff_redacts_secret_form_fields_and_prints_set_differences():
    diff = empty_diff(
        identical=False,
        services=EntryDiff(missing=[SnapshotService("Mosh", 60001, 60010, 60001, "UDP")]),
        forwards=EntryDiff(extra=[SnapshotForward("x", "host-b", "aa:bb:cc:dd:ee:01")]),
        forms={"wconfig_unified": [FormFieldDiff(field="wpa_key", dump="secret1", live="secret2")]},
        firmware_changed=True,
    )
    output = capture(fmt.print_snapshot_diff, diff)
    assert "- missing service Mosh UDP 60001-60010 -> 60001" in output
    assert "+ extra forward x -> host-b (aa:bb:cc:dd:ee:01)" in output
    assert "wconfig_unified:\n  ~ wpa_key: [redacted] -> [redacted]" in output
    assert "secret1" not in output
    assert "Firmware differs" in output


def test_print_snapshot_diff_says_so_when_identical():
    assert capture(fmt.print_snapshot_diff, empty_diff()) == "No differences.\n"
    only_fw = capture(fmt.print_snapshot_diff, empty_diff(firmware_changed=True))
    assert only_fw == "No configuration differences.\nFirmware differs between dump and router.\n"


def test_print_snapshot_diff_reports_reservations_and_absent_form_values():
    diff = empty_diff(
        identical=False,
        reservations=ReservationDiff(
            missing=[SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70")],
            changed=[ReservationChange(mac="02:0a:0b:0c:0d:02", dump_ip="192.168.1.64", live_ip="192.168.1.99")],
            extra=[SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65")],
        ),
        forms={"dosprotect": [FormFieldDiff(field="algsip", dump="on", live=None)]},
    )
    output = capture(fmt.print_snapshot_diff, diff)
    assert "- missing reservation 192.168.1.70 for 02:0a:0b:0c:0d:03\n" in output
    assert "~ reservation 02:0a:0b:0c:0d:02: 192.168.1.99 -> 192.168.1.64\n" in output
    assert "+ extra reservation 192.168.1.65 for 02:0a:0b:0c:0d:01\n" in output
    assert "  ~ algsip: <absent> -> on\n" in output


def test_display_diff_redacts_form_values_for_json_unless_include_secrets():
    diff = empty_diff(
        identical=False,
        forms={"wconfig_unified": [FormFieldDiff(field="wpa_key", dump="secret1", live=None),
                                   FormFieldDiff(field="algsip", dump="on", live="off")]},
    )
    shown = fmt.display_diff(diff, include_secrets=False)
    assert shown["identical"] is False
    assert shown["firmwareChanged"] is False
    assert shown["services"] == {"missing": [], "extra": []}
    assert shown["reservations"] == {"missing": [], "changed": [], "extra": []}
    assert shown["forms"]["wconfig_unified"][0] == {"field": "wpa_key", "dump": "[redacted]"}
    assert shown["forms"]["wconfig_unified"][1] == {"field": "algsip", "dump": "on", "live": "off"}
    raw = fmt.display_diff(diff, include_secrets=True)
    assert raw["forms"]["wconfig_unified"][0] == {"field": "wpa_key", "dump": "secret1"}
    assert "secret1" not in json.dumps(shown)


# --- restore plan / results (from format.test.ts) ---------------------------------------------


def test_print_restore_plan_lists_steps_with_blocked_reasons():
    output = capture(fmt.print_restore_plan, [
        RestoreStep(order=1, kind="add-service", page="services", description="add service Mosh", button="Add",
                    display_payload={"Service": "Mosh"}),
        RestoreStep(order=2, kind="add-forward", page="apphosting", description="add forward Nope",
                    blocked="service 'Nope' not offered"),
    ])
    assert output.startswith("[1] services add-service: add service Mosh\n")
    assert '    payload: {"Service":"Mosh"}\n' in output
    assert "    blocked: service 'Nope' not offered\n" in output


def test_print_restore_plan_strips_ansi_escape_sequences_from_router_derived_description():
    output = capture(fmt.print_restore_plan, [
        RestoreStep(order=1, kind="form", page="dosprotect", description=f"{ESC}[31mred{ESC}[0m"),
    ])
    assert ESC not in output
    assert "red" in output


def test_print_restore_plan_shows_deferred_reason_and_follow_up_payload():
    forward = SnapshotForward("Mosh", "host-b", "02:0a:0b:0c:0d:04")
    output = capture(fmt.print_restore_plan, [
        RestoreStep(
            order=1, kind="reserve", page="ipalloc",
            description="reserve 192.168.1.67 for 02:0a:0b:0c:0d:04 (currently DHCP)",
            button="Allocate_02:0a:0b:0c:0d:04", display_payload={"Allocate_02:0a:0b:0c:0d:04": "Allocate"},
            follow_up=RestoreFollowUp(page="ipalloc", select_name="alloc_02:0a:0b:0c:0d:04",
                                      option_value="192.168.1.67", button="Save"),
        ),
        RestoreStep(order=2, kind="add-forward", page="apphosting", description="add forward Mosh -> host-b",
                    deferred=RestoreDeferredForward(forward=forward, reason="service Mosh is added in this run")),
    ])
    assert '    payload: {"Allocate_02:0a:0b:0c:0d:04":"Allocate"}\n' in output
    assert '    then: {"alloc_02:0a:0b:0c:0d:04":"192.168.1.67","Save":"Save"}\n' in output
    assert (
        "[2] apphosting add-forward: add forward Mosh -> host-b\n    deferred: service Mosh is added in this run\n"
    ) in output


def test_print_restore_step_result_prints_status_code_and_location():
    result = RestoreStepResult(order=1, page="services", kind="add-service", description="d", status="applied",
                               status_code=302, location="/cgi-bin/services.ha")
    assert capture(fmt.print_restore_step_result, result) == "[1] applied services (302 -> /cgi-bin/services.ha)\n"
    failed = RestoreStepResult(order=2, page="apphosting", kind="add-forward", description="d", status="failed",
                               error="boom")
    assert capture(fmt.print_restore_step_result, failed) == "[2] failed apphosting (boom)\n"
    blocked = RestoreStepResult(order=3, page="x", kind="skip", description="d", status="blocked")
    assert capture(fmt.print_restore_step_result, blocked) == "[3] blocked x\n"


def test_display_restore_steps_drops_raw_payload_and_redacts_assignments():
    steps = [RestoreStep(
        order=1, kind="form", page="wconfig", description="apply", button="Save",
        assignments=["wpa_key=secret", "channel=6", "noequals"],
        display_payload={"wpa_key": "[redacted]"}, raw_payload={"nonce": "n", "wpa_key": "secret"},
    )]
    shown = fmt.display_restore_steps(steps, include_secrets=False)
    assert shown == [{
        "order": 1, "kind": "form", "page": "wconfig", "description": "apply", "button": "Save",
        "assignments": ["wpa_key=[redacted]", "channel=6", "noequals"],
        "displayPayload": {"wpa_key": "[redacted]"},
    }]
    raw = fmt.display_restore_steps(steps, include_secrets=True)
    assert raw[0]["assignments"] == ["wpa_key=secret", "channel=6", "noequals"]
    assert "rawPayload" not in raw[0]


def test_summarize_fetch_failures_drops_parsed_pages():
    failures = [ParsedPageResult(page="diag", ok=False, status_code=500, error="x",
                                 parsed=ParsedPage(page="diag", title="", heading="", values={"k": "secret"}))]
    assert fmt.summarize_fetch_failures(failures) == [{"page": "diag", "ok": False, "statusCode": 500, "error": "x"}]
