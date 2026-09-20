# ruff: noqa: E501  -- inline HTML fixtures are kept on one line to mirror the TS tests verbatim
"""Port of tests/parser.test.ts, plus the parse_page expectations that snapshot/restore/mutations
tests in the TS repo relied on (same inline HTML, asserted here at the parser boundary)."""

from dataclasses import asdict

import pytest

from bgwcli.parser import looks_like_login, parse_devices, parse_logs, parse_page, parse_sitemap
from bgwcli.types import (
    ParsedButton,
    ParsedForm,
    ParsedLink,
    ParsedTextarea,
    ParsedValueEntry,
    SelectOption,
    SitemapEntry,
    to_json_dict,
)

# --- parser.test.ts ------------------------------------------------------------------------


def test_parse_sitemap_extracts_cgi_page_names_and_labels():
    entries = parse_sitemap(
        """
        <a href="/cgi-bin/sysinfo.ha">System Information</a>
        <a href="https://192.168.1.254/cgi-bin/wconfig_unified.ha">Wi-Fi Configure</a>
        """
    )
    assert entries == [
        SitemapEntry(page="sysinfo", label="System Information", href="/cgi-bin/sysinfo.ha"),
        SitemapEntry(
            page="wconfig_unified", label="Wi-Fi Configure", href="https://192.168.1.254/cgi-bin/wconfig_unified.ha"
        ),
    ]


def test_parse_sitemap_skips_non_ha_links_and_dedupes_and_sorts():
    entries = parse_sitemap(
        """
        <a href="/cgi-bin/speed.ha">Speed Test</a>
        <a href="/cgi-bin/diag.ha">Troubleshoot</a>
        <a href="/cgi-bin/diag.ha">Troubleshoot</a>
        <a href="/cgi-bin/diag.ha?x=1">Not a plain page</a>
        <a href="/help.html">Help</a>
        <a href="/cgi-bin/logs.ha"></a>
        """
    )
    assert [(e.page, e.label) for e in entries] == [("diag", "Troubleshoot"), ("speed", "Speed Test")]


def test_parse_page_redacts_sensitive_values_by_default():
    parsed = parse_page(
        "wifi",
        """
        <title>Wi-Fi</title>
        <h1>Wi-Fi</h1>
        <table><tr><td>SSID</td><td>Home</td></tr></table>
        <input name="wpa_key" value="super-secret">
        """,
    )
    assert parsed.title == "Wi-Fi"
    assert parsed.heading == "Wi-Fi"
    assert parsed.values["SSID"] == "Home"
    assert parsed.values["Field wpa_key"] == "[redacted]"
    assert parsed.fields[0].sensitive is True


def test_parse_page_can_include_secrets_when_explicitly_requested():
    parsed = parse_page("wifi", '<input name="wpa_key" value="super-secret">', include_secrets=True)
    assert parsed.values["Field wpa_key"] == "super-secret"


def test_parse_page_extracts_buttons_textareas_and_forms():
    parsed = parse_page(
        "diag",
        """
        <form method="post" action="/cgi-bin/diag.ha">
          <input name="nonce" value="abc">
          <textarea name="progress">ready</textarea>
          <button name="Ping" value="Ping">Ping</button>
        </form>
        """,
    )
    assert parsed.textareas == [ParsedTextarea(name="progress", value="ready", sensitive=False)]
    assert parsed.buttons[0] == ParsedButton(name="Ping", type="submit", value="Ping", label="Ping", sensitive=False)
    assert parsed.forms[0] == ParsedForm(
        method="POST",
        action="/cgi-bin/diag.ha",
        field_names=["nonce"],
        select_names=[],
        textarea_names=["progress"],
        button_names=["Ping"],
    )
    # nonce is captured as a (sensitive, hidden-style) field, but never surfaces in values.
    assert parsed.fields[0].name == "nonce" and parsed.fields[0].sensitive is True
    assert "Field nonce" not in parsed.values


def test_parse_page_keeps_submit_inputs_out_of_editable_fields():
    parsed = parse_page(
        "packetfilter",
        """
        <form method="post" action="/cgi-bin/packetfilter.ha">
          <input name="nonce" value="abc">
          <input type="submit" name="Enable" value="Enable Packet Filters">
        </form>
        """,
    )
    assert [f.name for f in parsed.fields] == ["nonce"]
    assert [b.name for b in parsed.buttons] == ["Enable"]
    assert parsed.buttons[0].value == "Enable Packet Filters"
    assert parsed.buttons[0].label == "Enable Packet Filters"
    assert parsed.forms[0].field_names == ["nonce"]
    assert parsed.forms[0].button_names == ["Enable"]


def test_parse_page_gives_speed_test_history_stable_columns():
    parsed = parse_page(
        "speed",
        """
        <table>
          <tr><td>05/23/2026 16:37:05</td><td>upstream</td><td>1242.570000</td><td>28</td><td>40.000000</td><td>Success</td></tr>
        </table>
        """,
    )
    assert parsed.tables[0] == {
        "Time": "05/23/2026 16:37:05",
        "Direction": "upstream",
        "Mbps": "1242.570000",
        "Server": "28",
        "Latency ms": "40.000000",
        "Result": "Success",
    }


def test_parse_devices_handles_key_value_device_table_shape():
    devices = parse_devices(
        """
        <table>
          <tr><td>MAC Address</td><td>aa:bb:cc:dd:ee:ff</td></tr>
          <tr><td>IPv4 Address / Name</td><td>192.168.1.10 / laptop</td></tr>
          <tr><td>Last Activity</td><td>today</td></tr>
          <tr><td>Status</td><td>on</td></tr>
          <tr><td>Allocation</td><td>dhcp</td></tr>
          <tr><td>Connection Type</td><td>Wi-Fi 5 GHz Radio-1 Type: Home</td></tr>
          <tr><td>Mesh Client</td><td>No</td></tr>
        </table>
        """
    )
    assert to_json_dict(devices[0]) == {
        "status": "on",
        "name": "laptop",
        "ip": "192.168.1.10",
        "mac": "aa:bb:cc:dd:ee:ff",
        "connection": "Wi-Fi 5 GHz Radio-1 Type: Home",
        "allocation": "dhcp",
        "lastActivity": "today",
        "meshClient": "No",
    }


def test_parse_devices_handles_wide_table_shape_and_multiple_devices():
    devices = parse_devices(
        """
        <table>
          <tr><th>Status</th><th>IPv4 Address / Name</th><th>IPv6</th><th>MAC Address</th><th>Connection Type</th></tr>
          <tr><td>on</td><td>192.168.1.5 / tv</td><td></td><td>11:22:33:44:55:66</td><td>Ethernet</td></tr>
          <tr><td>off</td><td>printer</td><td></td><td>aa:aa:aa:aa:aa:aa</td></tr>
        </table>
        <table><tr><td>Unrelated</td><td>x</td><td>y</td></tr></table>
        """
    )
    assert [(d.status, d.name, d.ip, d.mac, d.connection) for d in devices] == [
        ("on", "tv", "192.168.1.5", "11:22:33:44:55:66", "Ethernet"),
        ("off", "printer", "Unknown", "aa:aa:aa:aa:aa:aa", "Unknown"),
    ]


def test_parse_devices_key_value_shape_splits_multiple_devices_on_mac_rows():
    devices = parse_devices(
        """
        <table>
          <tr><td>MAC Address</td><td>aa:aa:aa:aa:aa:01</td></tr>
          <tr><td>Name</td><td>one</td></tr>
          <tr><td>Status</td><td>on</td></tr>
          <tr><td>MAC Address</td><td>aa:aa:aa:aa:aa:02</td></tr>
          <tr><td>IPv4 Address / Name</td><td>10.0.0.2 / two</td></tr>
          <tr><td>IPv6 Address</td><td>fe80::2</td></tr>
          <tr><td>Type</td><td>phone</td></tr>
        </table>
        """
    )
    assert [(d.name, d.ip, d.mac) for d in devices] == [("one", "", "aa:aa:aa:aa:aa:01"), ("two", "10.0.0.2", "aa:aa:aa:aa:aa:02")]
    assert devices[1].ipv6 == "fe80::2" and devices[1].device_type == "phone"
    assert devices[0].ipv6 is None


def test_parse_page_preserves_metric_label_for_blank_first_table_header():
    parsed = parse_page(
        "voice",
        """
        <table>
          <tr><th></th><th>Line 1</th><th>Line 2</th></tr>
          <tr><td>Status</td><td>Down</td><td>Down</td></tr>
        </table>
        """,
    )
    assert parsed.tables[0] == {"Metric": "Status", "Line 1": "Down", "Line 2": "Down"}


def test_parse_page_generic_table_uses_metric_for_blank_first_header_and_drops_blank_others():
    parsed = parse_page(
        "sysinfo",
        """
        <table>
          <tr><th></th><th>A</th><th></th><th>C</th></tr>
          <tr><td>row</td><td>1</td><td>hidden</td><td>3</td></tr>
          <tr><td>short</td><td>row</td></tr>
        </table>
        """,
    )
    assert parsed.tables == [{"Metric": "row", "A": "1", "C": "3"}]


def test_parse_page_reads_diagnostic_status_rows_and_checked_protocol():
    parsed = parse_page(
        "diag",
        """
        <table class="diag">
          <tr><th scope="row">Ethernet</th><td>-</td><td><input type="submit" name="EthDetails" value="Details"></td></tr>
          <tr><th scope="row">Authentication</th><td>Pass</td><td><input type="submit" name="AuthDetails" value="Details"></td></tr>
          <tr><th scope="row">IP</th><td>Skipped</td><td><input type="submit" name="IPDetails" value="Details"></td></tr>
          <tr><th scope="row">DNS</th><td>Fail</td><td><input type="submit" name="DNSDetails" value="Details"></td></tr>
        </table>
        <input type="radio" name="protopref" value="IPv4" checked>
        <input type="radio" name="protopref" value="IPv6">
        """,
    )
    assert parsed.tables == [
        {"Test": "Ethernet", "Status": "-"},
        {"Test": "Authentication", "Status": "Pass"},
        {"Test": "IP", "Status": "Skipped"},
        {"Test": "DNS", "Status": "Fail"},
    ]
    assert parsed.values["Field protopref"] == "IPv4"
    assert [(f.name, f.type, f.value, f.checked) for f in parsed.fields] == [
        ("protopref", "radio", "IPv4", True),
        ("protopref", "radio", "IPv6", False),
    ]


def test_parse_page_extracts_sitemap_links_as_table_rows():
    parsed = parse_page(
        "sitemap",
        """
        <title>Site Map</title>
        <ul>
          <li><a href="/cgi-bin/diag.ha">Troubleshoot</a></li>
          <li><a href="/cgi-bin/speed.ha">Speed Test</a></li>
        </ul>
        """,
    )
    assert parsed.tables == [
        {"Page": "diag", "Label": "Troubleshoot", "Href": "/cgi-bin/diag.ha"},
        {"Page": "speed", "Label": "Speed Test", "Href": "/cgi-bin/speed.ha"},
    ]


def test_parse_page_extracts_page_description_blocks():
    parsed = parse_page(
        "pshosts",
        """
        <title>Public Subnet Hosts</title>
        <div class="desc">You must configure a Public Subnet first.</div>
        <div class="desc">You must configure a Public Subnet first.</div>
        <div class="page-desc">Second note.</div>
        <div class="description">Not a desc block.</div>
        """,
    )
    assert parsed.values["Description"] == "You must configure a Public Subnet first."
    assert parsed.values["Description 2"] == "Second note."
    assert "Description 3" not in parsed.values


def test_looks_like_login_ignores_configuration_pages_with_password_controls():
    assert (
        looks_like_login(
            """
            <title>Access Code</title>
            <form action="/cgi-bin/routerpasswd.ha">
              <input id="password" name="old_password" value="">
              <input name="new_password" value="">
            </form>
            """
        )
        is False
    )
    assert (
        looks_like_login(
            """
            <title>Login</title>
            <form action="/cgi-bin/login.ha">
              <input id="password" name="password">
            </form>
            """
        )
        is True
    )


def test_looks_like_login_matches_title_access_code_text_or_login_form():
    assert looks_like_login("<title>login</title>") is True
    assert looks_like_login("<p>Access Code Required</p>") is True
    assert looks_like_login('<form action="/cgi-bin/login.ha"></form><input id="password">') is True
    assert looks_like_login('<form action="/cgi-bin/login.ha"><input name="password"></form>') is False
    assert looks_like_login("<title>Login Page</title>") is False


def test_parse_page_extracts_wifi_current_channels_and_widths():
    parsed = parse_page(
        "wconfig_unified",
        """
        <h2>2.4 GHz Wi-Fi Channel Selection</h2>
        <table><tr><th>Current Channel</th><td>6 (20 MHz)</td><td>Automatic</td></tr></table>
        <h2>5 GHz Wi-Fi Channel Selection</h2>
        <table><tr><th>Current Channel</th><td>60 (80 MHz),161 (80 MHz)</td><td>Automatic</td></tr></table>
        """,
    )
    assert {"Radio": "2.4 GHz", "Current Channel": "6", "Channel Width": "20 MHz", "Mode": "Automatic"} in parsed.tables
    assert {"Radio": "5 GHz low-band", "Current Channel": "60", "Channel Width": "80 MHz", "Mode": "Automatic"} in parsed.tables
    assert {"Radio": "5 GHz high-band", "Current Channel": "161", "Channel Width": "80 MHz", "Mode": "Automatic"} in parsed.tables


def test_parse_page_wifi_channels_without_width_and_single_5ghz_channel():
    parsed = parse_page(
        "wconfig_unified",
        """
        <form>
        <h2>2.4 GHz</h2>
        <table><tr><th>Current Channel</th><td>Off</td><td>Manual</td></tr></table>
        </form>
        <h2>5 GHz</h2>
        <table><tr><th>Current Channel</th><td>149 (80 MHz)</td><td>Automatic</td></tr></table>
        <h2>Other</h2>
        <table><tr><th>Current Channel</th><td>1 (20 MHz)</td><td>x</td></tr></table>
        """,
    )
    assert parsed.tables == [
        {"Radio": "2.4 GHz", "Current Channel": "Off", "Channel Width": "", "Mode": "Manual"},
        {"Radio": "5 GHz", "Current Channel": "149", "Channel Width": "80 MHz", "Mode": "Automatic"},
    ]


def test_parse_page_preserves_duplicate_and_blank_router_values_with_section_context():
    parsed = parse_page(
        "broadbandstatistics",
        """
        <h2>IPv4</h2><table><tr><th>Primary DNS</th><td>192.0.2.1</td></tr></table>
        <h2>IPv6</h2><table><tr><th>Primary DNS</th><td></td></tr></table>
        """,
    )
    assert parsed.value_entries == [
        ParsedValueEntry(section="IPv4", label="Primary DNS", value="192.0.2.1"),
        ParsedValueEntry(section="IPv6", label="Primary DNS", value=""),
    ]
    # values keeps the last non-empty value only; blank entries never overwrite.
    assert parsed.values["Primary DNS"] == "192.0.2.1"


def test_parse_page_value_entries_split_default_values_and_strip_colons():
    parsed = parse_page(
        "wconfig_unified",
        """
        <table>
          <tr><td>Home SSID Enable Default: On</td><td>Off On</td></tr>
          <tr><td>Password Default: hunter2</td><td>abc</td></tr>
          <tr><td>Serial Number:</td><td>SN123</td></tr>
          <tr><td></td><td>orphan</td></tr>
          <tr><td>With control</td><td><input name="x" value="1"></td></tr>
        </table>
        """,
    )
    assert parsed.value_entries == [
        # The TS regex keeps the word "Default" in the label; only the trailing value is split off.
        ParsedValueEntry(section="", label="Home SSID Enable Default", value="Off On", default_value="On"),
        ParsedValueEntry(section="", label="Password Default", value="[redacted]", default_value="[redacted]"),
        ParsedValueEntry(section="", label="Serial Number", value="SN123"),
    ]
    assert parsed.values == {
        "Home SSID Enable Default": "Off On", "Password Default": "[redacted]", "Serial Number": "SN123", "Field x": "1",
    }


def test_parse_page_preserves_option_labels_state_control_metadata_and_links():
    parsed = parse_page(
        "wconfig",
        """
        <a href="/cgi-bin/wconfig_unified.ha">Basic Wi-Fi</a>
        <a href="https://example.com/help">Support</a>
        <input name="power" value="100" readonly>
        <select name="standard" disabled>
          <option value="ax" selected>Wi-Fi 6</option>
          <option value="ac" disabled>Wi-Fi 5</option>
        </select>
        """,
    )
    assert parsed.fields[0].name == "power"
    assert parsed.fields[0].read_only is True
    assert parsed.fields[0].disabled is None
    select = parsed.selects[0]
    assert (select.name, select.value, select.disabled) == ("standard", "ax", True)
    assert select.options == ["ax", "ac"]
    assert select.option_details == [
        SelectOption(value="ax", label="Wi-Fi 6", selected=True, disabled=False),
        SelectOption(value="ac", label="Wi-Fi 5", selected=False, disabled=True),
    ]
    assert parsed.links == [
        ParsedLink(label="Basic Wi-Fi", href="/cgi-bin/wconfig_unified.ha", kind="router-page", page="wconfig_unified"),
        ParsedLink(label="Support", href="https://example.com/help", kind="external"),
    ]


def test_parse_page_links_fall_back_to_title_then_href_and_dedupe():
    parsed = parse_page(
        "home",
        """
        <a href="/x.html" title="Titled"></a>
        <a href="/y.html"></a>
        <a href="/y.html"></a>
        <a name="anchor-only">no href</a>
        """,
    )
    assert parsed.links == [
        ParsedLink(label="Titled", href="/x.html", kind="other"),
        ParsedLink(label="/y.html", href="/y.html", kind="other"),
    ]


def test_parse_page_gives_ethernet_ports_explicit_current_configuration_and_capabilities():
    parsed = parse_page(
        "etherlan",
        """
        <select name="enet1_port1_media"><option value="Auto" selected>Auto</option><option value="5G">5G full duplex</option></select>
        <select name="enet1_port1_mdix"><option value="Auto" selected>Auto</option><option value="On">On</option></select>
        """,
    )
    assert parsed.tables[0] == {
        "Port": "1",
        "Configured media": "Auto",
        "Configured MDI-X": "Auto",
        "Supported media modes": "Auto, 5G full duplex",
        "Supported MDI-X modes": "Auto, On",
    }
    assert len(parsed.tables) == 4
    assert parsed.tables[3] == {
        "Port": "4", "Configured media": "", "Configured MDI-X": "", "Supported media modes": "", "Supported MDI-X modes": "",
    }


def test_parse_page_labels_fiber_thresholds_with_their_measurement():
    parsed = parse_page(
        "fiberstat",
        """
        <h1>Rx Power&nbsp;&nbsp;Currently -141</h1>
        <table><tr><th></th><th>Low</th><th>High</th></tr><tr><td>Alarm</td><td>0 (Threshold -322)</td><td>0 (Threshold -70)</td></tr></table>
        """,
    )
    assert parsed.tables == [
        {
            "Measurement": "Rx Power",
            "Current": "-141",
            "State": "Alarm",
            "Low": "0 (Threshold -322)",
            "High": "0 (Threshold -70)",
        }
    ]
    assert parsed.values["Rx Power"] == "-141"
    assert parsed.heading == "Rx Power Currently -141"


def test_parse_page_captures_all_voice_statistics_table_shapes():
    parsed = parse_page(
        "voicestat",
        """
        <table>
          <tr><th>Line 1</th><th colspan="2">Last Call</th><th colspan="2">Cumulative</th></tr>
          <tr><th></th><th>Incoming</th><th>Outgoing</th><th>Incoming</th><th>Outgoing</th></tr>
          <tr><td>RTP Packet Loss</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>
        </table>
        <table>
          <tr><th>Line 2</th><th colspan="2">Last Call</th><th colspan="2">Cumulative</th></tr>
          <tr><th></th><th>Incoming</th><th>Outgoing</th><th>Incoming</th><th>Outgoing</th></tr>
          <tr><td>RTP Packet Loss</td><td>5</td><td>6</td><td>7</td><td>8</td></tr>
        </table>
        <table>
          <tr><th></th><th colspan="2">Line 1</th><th colspan="2">Line 2</th></tr>
          <tr><th></th><th>Current</th><th>Last</th><th>Current</th><th>Last</th></tr>
          <tr><td>Far-End Caller Information</td><td>caller-a</td><td>caller-b</td><td>caller-c</td><td>caller-d</td></tr>
        </table>
        <table>
          <tr><th></th><th>Line 1</th><th>Line 2</th></tr>
          <tr><td>Number of Calls</td><td>9</td><td>10</td></tr>
        </table>
        """,
    )
    assert len(parsed.tables) == 4
    assert parsed.tables[0] == {
        "Section": "Call statistics", "Line": "Line 1", "Metric": "RTP Packet Loss",
        "Last call incoming": "1", "Last call outgoing": "2", "Cumulative incoming": "3", "Cumulative outgoing": "4",
    }
    assert parsed.tables[1]["Line"] == "Line 2" and parsed.tables[1]["Cumulative outgoing"] == "8"
    assert parsed.tables[2] == {
        "Section": "Call summary", "Line": "Both", "Metric": "Far-End Caller Information",
        "Line 1 current": "[redacted]", "Line 1 last": "[redacted]", "Line 2 current": "[redacted]", "Line 2 last": "[redacted]",
    }
    assert parsed.tables[3] == {
        "Section": "Cumulative since last reset", "Line": "Both", "Metric": "Number of Calls", "Line 1": "9", "Line 2": "10",
    }


def test_parse_page_voice_status_redacts_sensitive_metrics_and_skips_blank_metric_rows():
    parsed = parse_page(
        "voiceconfig",
        """
        <table>
          <tr><th></th><th>Line 1</th><th>Line 2</th></tr>
          <tr><td>Phone Number</td><td>555-0100</td><td>555-0101</td></tr>
          <tr><td></td><td>x</td><td>y</td></tr>
          <tr><td>Status</td><td>Up</td><td>Down</td></tr>
        </table>
        """,
    )
    assert parsed.tables == [
        {"Metric": "Phone Number", "Line 1": "[redacted]", "Line 2": "[redacted]"},
        {"Metric": "Status", "Line 1": "Up", "Line 2": "Down"},
    ]


def test_parse_page_models_advanced_wifi_radio_and_ssid_configuration():
    parsed = parse_page(
        "wconfig",
        """
        <input name="power" value="100">
        <input name="ssidname11" value="Home">
        <input name="maxclients" value="80">
        <select name="wl80211on"><option value="on" selected>On</option></select>
        <select name="standard"><option value="g/n" selected>G/N</option></select>
        <select name="bandwidth"><option value="20" selected>20 MHz</option></select>
        <select name="channelplusauto"><option value="auto" selected>Automatic</option></select>
        <select name="ussidenable"><option value="on" selected>On</option></select>
        """,
    )
    radio = next(t for t in parsed.tables if t.get("Section") == "Radio" and t.get("Radio") == "2.4 GHz")
    assert radio == {
        "Section": "Radio", "Radio": "2.4 GHz", "Enabled": "on", "Standard": "g/n", "Bandwidth": "20",
        "Channel": "auto", "Power level (%)": "100",
    }
    ssid = next(t for t in parsed.tables if t.get("Section") == "SSID" and t.get("Network") == "Home" and t["Radio"] == "2.4 GHz")
    assert ssid["SSID"] == "Home" and ssid["Maximum clients"] == "80" and ssid["Enabled"] == "on"
    assert len(parsed.tables) == 5


def test_parse_page_mac_filter_tables():
    parsed = parse_page(
        "wmacauth",
        """
        <select name="wmacr1user"><option value="off" selected>Off</option></select>
        <select name="wmacr2user"><option value="allow" selected>Allow</option></select>
        """,
    )
    assert parsed.tables == [
        {"Radio": "2.4 GHz", "Network": "Home", "Filtering": "off"},
        {"Radio": "2.4 GHz", "Network": "Guest", "Filtering": ""},
        {"Radio": "5 GHz", "Network": "Home", "Filtering": "allow"},
    ]


def test_parse_logs_reads_first_table_rows_with_six_columns():
    logs = parse_logs(
        """
        <table>
          <tr><th>#</th><th>Time</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Reason</th></tr>
          <tr><td>1</td><td>10:00</td><td>1.1.1.1</td><td>2.2.2.2</td><td>TCP</td><td>blocked</td></tr>
          <tr><td>short</td><td>row</td></tr>
        </table>
        <table><tr><td>2</td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td></tr></table>
        """
    )
    assert [asdict(entry) for entry in logs] == [
        {"id": "1", "time": "10:00", "source": "1.1.1.1", "destination": "2.2.2.2", "protocol": "TCP", "reason": "blocked"}
    ]
    assert parse_logs("<p>no tables</p>") == []


# --- snapshot.test.ts / restore.test.ts / mutations.test.ts inline HTML ---------------------

SERVICES_HTML = """
<html><head><title>Custom Services</title></head><body>
<form method="post" action="/cgi-bin/services.ha">
<input type="hidden" name="nonce" value="abc">
<table>
<tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>
<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>Mosh</td><td>60001-60010</td><td>60001</td><td>UDP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>
</table>
<input type="text" name="Service" value="">
<input type="text" name="extMinPort" value="">
<input type="text" name="extMaxPort" value="">
<input type="text" name="intStartPort" value="">
<select name="protocol"><option value="TCP" selected>TCP</option><option value="UDP">UDP</option></select>
<input type="submit" name="Add" value="Add">
</form></body></html>"""


def test_services_page_tables_buttons_and_form_shape_for_snapshot():
    parsed = parse_page("services", SERVICES_HTML, include_secrets=True)
    assert parsed.title == "Custom Services"
    # 5 headers, the last blank -> 4 keys per row; the Remove cell contributes no text.
    assert parsed.tables == [
        {"Service Name": "custom_ssh", "Global Port Range": "2483-2483", "Base Host Port": "22", "Protocol": "TCP"},
        {"Service Name": "Mosh", "Global Port Range": "60001-60010", "Base Host Port": "60001", "Protocol": "UDP"},
    ]
    assert [(b.name, b.value, b.label, b.type) for b in parsed.buttons] == [
        ("Remove_1", "Remove", "Remove", "submit"),
        ("Remove_2", "Remove", "Remove", "submit"),
        ("Add", "Add", "Add", "submit"),
    ]
    assert [f.name for f in parsed.fields] == ["nonce", "Service", "extMinPort", "extMaxPort", "intStartPort"]
    assert parsed.fields[0].type == "hidden" and parsed.fields[0].value == "abc"
    assert [s.name for s in parsed.selects] == ["protocol"]
    assert parsed.selects[0].value == "TCP"
    assert parsed.forms == [
        ParsedForm(
            method="POST",
            action="/cgi-bin/services.ha",
            field_names=["nonce", "Service", "extMinPort", "extMaxPort", "intStartPort"],
            select_names=["protocol"],
            textarea_names=[],
            button_names=["Remove_1", "Remove_2", "Add"],
        )
    ]
    # Table rows with controls are excluded from key/value entries (2-cell rows only anyway).
    assert parsed.value_entries == []


def test_services_page_with_an_extra_unrelated_table_flattens_rows_into_one_list():
    port_status = (
        "<table><tr><th>Port</th><th>Status</th><th>Notes</th></tr>"
        "<tr><td>80</td><td>open</td><td>-</td></tr><tr><td>443</td><td>open</td><td>-</td></tr></table>"
    )
    html = SERVICES_HTML.replace("<table>\n<tr><th>Service Name</th>", f"{port_status}<table>\n<tr><th>Service Name</th>")
    parsed = parse_page("services", html, include_secrets=True)
    assert parsed.tables[0] == {"Port": "80", "Status": "open", "Notes": "-"}
    assert parsed.tables[2]["Service Name"] == "custom_ssh"
    assert len(parsed.tables) == 4


def test_services_header_only_table_yields_no_rows():
    html = SERVICES_HTML.replace(
        '<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>\n',
        "",
    ).replace(
        '<tr><td>Mosh</td><td>60001-60010</td><td>60001</td><td>UDP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>\n',
        "",
    )
    parsed = parse_page("services", html, include_secrets=True)
    assert parsed.tables == []
    assert [b.name for b in parsed.buttons] == ["Add"]


APPHOSTING_HTML = """
<html><head><title>NAT/Gaming</title></head><body>
<form method="post" action="/cgi-bin/apphosting.ha">
<input type="hidden" name="nonce" value="abc">
<table>
<tr><th>Service</th><th>Needed by Device</th><th></th></tr>
<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>Mosh</td><td>host-a</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>
</table>
<select name="service"><option value="custom_ssh">custom_ssh</option><option value="Mosh">Mosh</option></select>
<select name="device">
<option value="aa:bb:cc:dd:ee:01">host-b</option>
<option value="aa:bb:cc:dd:ee:02">host-a</option>
<option value="aa:bb:cc:dd:ee:03">watch</option>
<option value="aa:bb:cc:dd:ee:04">watch</option>
</select>
<input type="submit" name="Add" value="Add">
</form></body></html>"""


def test_apphosting_page_device_select_keeps_duplicate_labels_and_first_option_default():
    parsed = parse_page("apphosting", APPHOSTING_HTML, include_secrets=True)
    assert parsed.tables == [
        {"Service": "custom_ssh", "Needed by Device": "host-b"},
        {"Service": "Mosh", "Needed by Device": "host-a"},
    ]
    device = next(s for s in parsed.selects if s.name == "device")
    assert device.value == "aa:bb:cc:dd:ee:01"  # no option selected -> first option
    assert [(o.value, o.label) for o in device.option_details] == [
        ("aa:bb:cc:dd:ee:01", "host-b"),
        ("aa:bb:cc:dd:ee:02", "host-a"),
        ("aa:bb:cc:dd:ee:03", "watch"),
        ("aa:bb:cc:dd:ee:04", "watch"),
    ]
    assert [b.name for b in parsed.buttons] == ["Remove_1", "Remove_2", "Add"]
    # Two 2-column key/value-looking rows exist but the table has a 3-header row, so no value entries
    # from the 3-cell rows; nothing is 2 cells wide here.
    assert parsed.value_entries == []


DOSPROTECT_HTML = """
<html><head><title>Firewall Advanced</title></head><body>
<form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="abc">
<select name="flood_protect"><option value="on" selected>On</option><option value="off">Off</option></select>
<select name="disabled_thing" disabled><option value="x" selected>x</option></select>
<input type="checkbox" name="reflexive" value="on" checked>
<input type="checkbox" name="unchecked_box">
<input type="submit" name="Save" value="Save">
</form></body></html>"""


def test_dosprotect_page_checkbox_and_disabled_select_flags():
    parsed = parse_page("dosprotect", DOSPROTECT_HTML, include_secrets=True)
    by_name = {f.name: f for f in parsed.fields}
    assert by_name["reflexive"].type == "checkbox" and by_name["reflexive"].checked is True
    assert by_name["reflexive"].value == "on"
    # A checkbox with no value attribute parses as value "" and unchecked; it is still a field.
    assert by_name["unchecked_box"].value == "" and by_name["unchecked_box"].checked is False
    assert by_name["unchecked_box"].disabled is None
    selects = {s.name: s for s in parsed.selects}
    assert selects["flood_protect"].disabled is None and selects["flood_protect"].value == "on"
    assert selects["disabled_thing"].disabled is True
    assert parsed.values["Field flood_protect"] == "on"
    assert parsed.values["Field reflexive"] == "on"
    assert "Field unchecked_box" not in parsed.values
    assert "Field nonce" not in parsed.values
    assert [b.name for b in parsed.buttons] == ["Save"]


def test_radio_group_keeps_each_control_with_its_own_checked_state():
    html = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="radio" name="mode" value="a">
<input type="radio" name="mode" value="b" checked>
<input type="radio" name="mode" value="c">
<input type="radio" name="other" value="x">
<input type="submit" name="Save" value="Save"></form></body></html>"""
    parsed = parse_page("dosprotect", html, include_secrets=True)
    assert [(f.name, f.value, f.checked) for f in parsed.fields] == [
        ("mode", "a", False), ("mode", "b", True), ("mode", "c", False), ("other", "x", False),
    ]
    assert parsed.values["Field mode"] == "b"
    assert "Field other" not in parsed.values
    assert parsed.forms[0].field_names == ["mode", "other"]


def test_escaped_unchecked_sentinel_in_attribute_is_decoded():
    html = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="checkbox" name="weird" value="&lt;unchecked&gt;" checked>
<input type="submit" name="Save" value="Save"></form></body></html>"""
    parsed = parse_page("dosprotect", html, include_secrets=True)
    assert parsed.fields[0].value == "<unchecked>"


def test_sysinfo_software_version_lands_in_values():
    parsed = parse_page(
        "sysinfo",
        "<html><head><title>System Information</title></head><body>"
        "<table><tr><td>Software Version</td><td>4.27.7</td></tr></table></body></html>",
        include_secrets=True,
    )
    assert parsed.values["Software Version"] == "4.27.7"
    assert parsed.tables == []


IPALLOC_HTML = """
<html><head><title>IP Allocation</title></head><body>
<form method="post" action="/cgi-bin/ipalloc.ha">
<input type="hidden" name="nonce" value="abc">
<table>
<tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Status</th><th>Allocation</th><th>Action</th></tr>
<tr><td>192.168.1.64</td><td>02:0A:0B:0C:0D:02</td><td>on</td><td>Fixed Allocation</td><td><input type="submit" name="Allocate_02:0a:0b:0c:0d:02" value="Allocate"></td></tr>
<tr><td>watch</td><td>02:0a:0b:0c:0d:04</td><td>off</td><td>DHCP Allocation</td><td><input type="submit" name="Allocate_02:0a:0b:0c:0d:04" value="Allocate"></td></tr>
<tr><td>192.168.1.70</td><td>02:0a:0b:0c:0d:03</td><td>on</td><td>Fixed Allocation</td><td><input type="submit" name="Allocate_02:0a:0b:0c:0d:03" value="Allocate"></td></tr>
</table>
</form></body></html>"""


def test_ipalloc_page_rows_keep_control_cells_as_empty_strings():
    parsed = parse_page("ipalloc", IPALLOC_HTML, include_secrets=True)
    assert len(parsed.tables) == 3
    assert parsed.tables[0] == {
        "IPv4 Address / Name": "192.168.1.64",
        "MAC Address": "02:0A:0B:0C:0D:02",
        "Status": "on",
        "Allocation": "Fixed Allocation",
        "Action": "",
    }
    assert [b.name for b in parsed.buttons] == [
        "Allocate_02:0a:0b:0c:0d:02", "Allocate_02:0a:0b:0c:0d:04", "Allocate_02:0a:0b:0c:0d:03",
    ]


def test_ipalloc_entry_page_select_by_id_and_name_with_disabled_option():
    html = """<html><body><form method="post" action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">
<select id="alloc" name="alloc_aa:bb:cc:dd:ee:ff" size="8"><option value="normal" selected>Address from DHCP pool</option><option value="192.168.1.67" disabled>Private fixed:192.168.1.67</option></select>
<input type="submit" name="Save" value="Save"><input type="submit" name="Cancel" value="Cancel"></form></body></html>"""
    parsed = parse_page("ipalloc", html, include_secrets=True)
    select = parsed.selects[0]
    assert select.name == "alloc_aa:bb:cc:dd:ee:ff"  # name wins over id
    assert select.value == "normal"
    assert select.option_details[1] == SelectOption(
        value="192.168.1.67", label="Private fixed:192.168.1.67", selected=False, disabled=True
    )
    assert parsed.forms[0].select_names == ["alloc_aa:bb:cc:dd:ee:ff"]
    assert parsed.forms[0].button_names == ["Save", "Cancel"]


WCONFIG_HTML = """
<html><head><title>Advanced Wi-Fi</title></head><body>
<form method="post" action="/cgi-bin/wconfig.ha">
<input type="hidden" name="nonce" value="abc">
<input type="text" name="ssidname11" value="EXAMPLE-NET">
<input type="text" name="key11" value="topsecret">
<input type="text" name="maxclients" value="80">
<input type="text" name="ssidname12" value="Guest" disabled>
<input type="text" name="WPSPIN5" value="">
<select name="wl80211on"><option value="on" selected>On</option><option value="off">Off</option></select>
<select name="gssidisolate" disabled><option value="on" selected>On</option></select>
<input type="submit" name="Update" value="Update">
<input type="submit" name="Save" value="Save...">
<input type="submit" name="Cancel" value="Cancel">
</form></body></html>"""


def test_wconfig_page_disabled_fields_secret_handling_and_submit_values():
    secret = parse_page("wconfig", WCONFIG_HTML, include_secrets=True)
    fields = {f.name: f for f in secret.fields}
    assert fields["key11"].value == "topsecret" and fields["key11"].sensitive is True
    assert fields["ssidname12"].disabled is True
    assert fields["WPSPIN5"].sensitive is True and fields["WPSPIN5"].value == ""
    assert [(b.name, b.value) for b in secret.buttons] == [("Update", "Update"), ("Save", "Save..."), ("Cancel", "Cancel")]
    redacted = parse_page("wconfig", WCONFIG_HTML)
    assert {f.name: f.value for f in redacted.fields}["key11"] == "[redacted]"
    assert redacted.values["Field key11"] == "[redacted]"
    assert redacted.values["Field ssidname11"] == "EXAMPLE-NET"
    # Advanced Wi-Fi page-specific table pulls straight from parsed controls.
    ssid_row = next(t for t in redacted.tables if t.get("Section") == "SSID" and t.get("Network") == "Home" and t["Radio"] == "2.4 GHz")
    assert ssid_row["SSID"] == "EXAMPLE-NET" and ssid_row["Maximum clients"] == "80"


def test_i6_mirror_page_every_control_kind_is_classified():
    html = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="abc">
<input type="hidden" name="hashpassword" value="deadbeef">
<input type="text" name="plain" value="1">
<input type="text" name="disabled_text" value="2" disabled>
<input type="button" name="btn_button" value="Button">
<input type="submit" name="btn_submit" value="Submit">
<input type="reset" name="btn_reset" value="Reset">
<input type="image" name="btn_image" value="Image">
<input type="checkbox" name="box_on" value="on" checked>
<input type="checkbox" name="box_off" value="on">
<input type="radio" name="rad_on" value="yes" checked>
<input type="radio" name="rad_off" value="no">
<select name="sel_on"><option value="a" selected>a</option><option value="b">b</option></select>
<select name="sel_off" disabled><option value="x" selected>x</option></select>
<textarea name="area_on">text</textarea>
<textarea name="area_off" disabled>other</textarea>
<input type="submit" name="Save" value="Save">
</form></body></html>"""
    parsed = parse_page("dosprotect", html, include_secrets=True)
    assert [f.name for f in parsed.fields] == [
        "nonce", "hashpassword", "plain", "disabled_text", "box_on", "box_off", "rad_on", "rad_off",
    ]
    assert [(b.name, b.type) for b in parsed.buttons] == [
        ("btn_button", "button"), ("btn_submit", "submit"), ("btn_reset", "reset"), ("btn_image", "image"), ("Save", "submit"),
    ]
    assert [(t.name, t.value, t.disabled) for t in parsed.textareas] == [("area_on", "text", None), ("area_off", "other", True)]
    assert parsed.forms[0].field_names == [
        "nonce", "hashpassword", "plain", "disabled_text", "box_on", "box_off", "rad_on", "rad_off",
    ]
    assert parsed.forms[0].button_names == ["btn_button", "btn_submit", "btn_reset", "btn_image", "Save"]
    assert "Field hashpassword" not in parsed.values and "Field nonce" not in parsed.values
    assert parsed.values["Field plain"] == "1"


WIFI_WARN_HTML = """<html><head><title>Wi-Fi Warning</title></head><body>
<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="n"><p>Warning: You have made a change to your Wi-Fi configuration.</p><input type="submit" name="Continue" value="Continue"></form>
<form method="post" action="/cgi-bin/wifiwarn_advanced.ha"><input type="hidden" name="nonce" value="n"><input type="submit" name="Cancel" value="Cancel"></form></body></html>"""


def test_wifi_warning_page_attributes_continue_to_its_owning_form():
    parsed = parse_page("wifiwarn_advanced", WIFI_WARN_HTML, include_secrets=True)
    assert parsed.title == "Wi-Fi Warning"
    assert [(f.action, f.button_names) for f in parsed.forms] == [
        ("/cgi-bin/wconfig.ha", ["Continue"]),
        ("/cgi-bin/wifiwarn_advanced.ha", ["Cancel"]),
    ]
    owning = next(f for f in parsed.forms if "Continue" in f.button_names)
    assert owning.action == "/cgi-bin/wconfig.ha"


def test_packetfilter_button_label_with_quotes_is_preserved():
    html = (
        '<html><body><form method="post" action="/cgi-bin/packetfilter.ha">'
        "<input type=\"submit\" name=\"AddDropRule\" value=\"Add a 'Drop' Rule\"></form></body></html>"
    )
    parsed = parse_page("packetfilter", html, include_secrets=True)
    assert parsed.buttons[0].label == "Add a 'Drop' Rule"
    assert parsed.forms[0].field_names == []


# --- structural / robustness ------------------------------------------------------------------


def test_button_element_name_fallbacks_and_value_precedence():
    parsed = parse_page(
        "diag",
        """
        <form action="/cgi-bin/x.ha">
        <button type="button">Click <b>me</b></button>
        <button name="n" value="">Labelled</button>
        <button id="only-id" disabled>Disabled</button>
        <button></button>
        <input type="submit" value="Go">
        <input type="reset">
        </form>
        """,
    )
    assert [(b.name, b.type, b.value, b.label, b.disabled) for b in parsed.buttons] == [
        ("Go", "submit", "Go", "Go", None),
        ("reset", "reset", "", "reset", None),
        ("Click me", "button", "Click me", "Click me", None),
        ("n", "submit", "", "Labelled", None),
        ("only-id", "submit", "Disabled", "Disabled", True),
        ("submit", "submit", "", "submit", None),
    ]
    # Form button names only count controls with a name or id.
    assert parsed.forms[0].button_names == ["n", "only-id"]
    assert parsed.forms[0].method == "GET"


def test_controls_outside_any_form_are_still_parsed_but_forms_stay_scoped():
    parsed = parse_page(
        "home",
        """
        <input name="outside" value="1">
        <form method="POST" action="/cgi-bin/a.ha"><input name="inside" value="2"><select id="s"><option>x</option></select></form>
        <form><textarea id="t">z</textarea></form>
        """,
    )
    assert [f.name for f in parsed.fields] == ["outside", "inside"]
    assert parsed.selects[0].name == "s" and parsed.selects[0].value == "x"  # value falls back to label
    assert parsed.forms == [
        ParsedForm("POST", "/cgi-bin/a.ha", ["inside"], ["s"], [], []),
        ParsedForm("GET", "", [], [], ["t"], []),
    ]


def test_inputs_without_name_or_id_are_ignored_and_type_case_is_preserved():
    parsed = parse_page("home", '<input value="anon"><input type="Text" name="t" value="v"><input type="SUBMIT" name="s" value="S">')
    assert [(f.name, f.type) for f in parsed.fields] == [("t", "Text")]
    assert [(b.name, b.type) for b in parsed.buttons] == [("s", "submit")]


def test_text_extraction_collapses_whitespace_and_decodes_entities():
    parsed = parse_page(
        "home",
        """
        <title> Home &amp;
           Status </title>
        <h1>Line&nbsp;<span>One</span></h1>
        <table><tr><td>A <b>&lt;b&gt;</b></td><td>  x
        y  </td></tr></table>
        <textarea name="ta"> a
          b </textarea>
        """,
    )
    assert parsed.title == "Home & Status"
    assert parsed.heading == "Line One"
    assert parsed.values["A <b>"] == "x y"
    assert parsed.textareas[0].value == "a b"


def test_script_style_and_comments_are_not_parsed_as_content():
    parsed = parse_page(
        "home",
        """
        <script>document.write('<input name="fake" value="1">');</script>
        <style>td { color: red; }</style>
        <!-- <input name="commented" value="2"> -->
        <table><tr><td>Item</td><td>Val<script>x</script></td></tr></table>
        """,
    )
    assert parsed.fields == []
    assert parsed.values["Item"] == "Val"


def test_unclosed_cells_rows_and_options_are_tolerated():
    parsed = parse_page(
        "home",
        """
        <table>
          <tr><td>Item<td>Value
          <tr><td>K2<td>V2
        </table>
        <select name="s"><option value="1">One<option value="2" selected>Two</select>
        """,
    )
    assert parsed.values["Item"] == "Value" and parsed.values["K2"] == "V2"
    assert parsed.selects[0].value == "2"
    assert [o.label for o in parsed.selects[0].option_details] == ["One", "Two"]


def test_nested_tables_are_separate_tables():
    parsed = parse_page(
        "home",
        """
        <table><tr><td>Outer</td><td>
          <table><tr><td>Inner</td><td>iv</td></tr></table>
        </td></tr></table>
        """,
    )
    assert parsed.values["Inner"] == "iv"
    assert parsed.values["Outer"] == "Inner iv"


def test_parse_page_dedupes_identical_table_rows_and_keeps_order():
    parsed = parse_page(
        "home",
        """
        <table>
          <tr><th>A</th><th>B</th><th>C</th></tr>
          <tr><td>1</td><td>2</td><td>3</td></tr>
          <tr><td>1</td><td>2</td><td>3</td></tr>
          <tr><td>4</td><td>5</td><td>6</td></tr>
        </table>
        """,
    )
    assert parsed.tables == [{"A": "1", "B": "2", "C": "3"}, {"A": "4", "B": "5", "C": "6"}]


def test_parse_page_two_column_tables_are_values_not_tables():
    parsed = parse_page("home", "<table><tr><th>K</th><th>V</th></tr><tr><td>a</td><td>b</td></tr></table>")
    assert parsed.tables == []
    assert parsed.values == {"K": "V", "a": "b"}


def test_parse_page_json_shape_matches_ts_keys():
    parsed = parse_page("home", '<title>T</title><input name="x" value="1" disabled><a href="/cgi-bin/y.ha">Y</a>')
    data = to_json_dict(parsed)
    assert set(data) == {
        "page", "title", "heading", "values", "valueEntries", "tables", "fields", "selects", "textareas",
        "buttons", "forms", "links",
    }
    assert data["fields"][0] == {"name": "x", "type": "text", "value": "1", "checked": False, "sensitive": False, "disabled": True}
    assert data["links"][0] == {"label": "Y", "href": "/cgi-bin/y.ha", "kind": "router-page", "page": "y"}
    assert data["valueEntries"] == []


def test_parse_page_empty_html():
    parsed = parse_page("home", "")
    assert parsed.page == "home" and parsed.title == "" and parsed.heading == ""
    assert parsed.values == {} and parsed.tables == [] and parsed.fields == [] and parsed.forms == []
    assert parsed.links == [] and parsed.value_entries == []


@pytest.mark.parametrize("page", ["etherlan", "fiberstat", "voice", "voiceconfig", "voicestat"])
def test_pages_with_dedicated_table_parsers_drop_generic_tables(page):
    parsed = parse_page(page, "<table><tr><th>A</th><th>B</th><th>C</th><th>D</th></tr><tr><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>")
    assert all("A" not in row for row in parsed.tables)


# --- sweep.test.ts fake client body (coordinator parity check) --------------------------------


@pytest.mark.parametrize("page", ["home", "diag", "wconfig"])
def test_sweep_fake_page_body_yields_two_values_and_one_value_entry(page):
    html = f"""<title>{page}</title><h1>{page}</h1>
          <table><tr><td>Status</td><td>Up</td></tr></table>
          <form action="/cgi-bin/{page}.ha">
            <input name="nonce" value="abc">
            <input name="target" value="example.com">
            <input type="submit" name="Ping" value="Ping">
          </form>"""
    parsed = parse_page(page, html)
    assert parsed.title == page and parsed.heading == page
    assert parsed.values == {"Status": "Up", "Field target": "example.com"}
    assert parsed.value_entries == [ParsedValueEntry(section=page, label="Status", value="Up")]
    assert [f.name for f in parsed.fields] == ["nonce", "target"]
    assert [b.name for b in parsed.buttons] == ["Ping"]
    assert parsed.forms == [ParsedForm("GET", f"/cgi-bin/{page}.ha", ["nonce", "target"], [], [], ["Ping"])]
    # Minimal variant without the form: one value, one entry (TS parity verified via bun oracle).
    bare = parse_page(page, "<html><head><title>Fake Page</title></head><body><h1>Fake</h1>"
                            "<table><tr><td>Status</td><td>Up</td></tr></table></body></html>")
    assert bare.values == {"Status": "Up"}
    assert bare.value_entries == [ParsedValueEntry(section="Fake", label="Status", value="Up")]
