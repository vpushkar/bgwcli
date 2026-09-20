"""Inline HTML fixtures shared by the integration tests.

Every constant is copied verbatim from the TypeScript test-suite (BGW320-CLI @ f8a6d5a:
tests/snapshot.test.ts, tests/restore.test.ts, tests/mutations.test.ts, tests/dumpfile.test.ts) so the
integration tests drive the real parser with exactly the markup the TS tests drove parsePage with.
Constants that exist in more than one TS file with different content carry the file in their name
(``RESTORE_*`` for restore.test.ts); the snapshot.test.ts versions carry the plain name.
"""

# ruff: noqa: E501 -- verbatim TS fixtures; wrapping them would change the markup under test

from __future__ import annotations

import re
from collections.abc import Sequence

# --- tests/snapshot.test.ts -------------------------------------------------------------------------

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

SYSINFO_HTML = """
<html><head><title>System Information</title></head><body>
<table><tr><td>Software Version</td><td>4.27.7</td></tr></table>
</body></html>"""

RADIO_GROUP_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="radio" name="mode" value="a">
<input type="radio" name="mode" value="b" checked>
<input type="radio" name="mode" value="c">
<input type="radio" name="other" value="x">
<input type="submit" name="Save" value="Save"></form></body></html>"""

# Identical in snapshot.test.ts and restore.test.ts.
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

# The submit value is HTML-escaped as a real page would emit it; the parser decodes it to the sentinel.
SENTINEL_COLLISION_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="checkbox" name="weird" value="&lt;unchecked&gt;" checked>
<input type="submit" name="Save" value="Save"></form></body></html>"""


def header_only(html: str) -> str:
    """TS: html.replace(/<tr><td>.*?<\\/tr>\\n/gs, "") — drop every data row, keep the header row."""
    return re.sub(r"<tr><td>.*?</tr>\n", "", html, flags=re.S)


# --- tests/restore.test.ts --------------------------------------------------------------------------

RESTORE_SERVICES_HTML = """<html><body><form method="post" action="/cgi-bin/services.ha">
<input type="hidden" name="nonce" value="n">
<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>
<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>Stale</td><td>1-1</td><td>1</td><td>TCP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr></table>
<input type="text" name="Service" value=""><input type="text" name="extMinPort" value=""><input type="text" name="extMaxPort" value="">
<input type="text" name="intStartPort" value=""><select name="protocol"><option value="TCP" selected>TCP</option><option value="UDP">UDP</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""

RESTORE_APPHOSTING_HTML = """<html><body><form method="post" action="/cgi-bin/apphosting.ha">
<input type="hidden" name="nonce" value="n">
<table><tr><th>Service</th><th>Needed by Device</th><th></th></tr>
<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr></table>
<select name="service"><option value="custom_ssh">custom_ssh</option><option value="Mosh">Mosh</option></select>
<select name="device"><option value="aa:bb:cc:dd:ee:01">host-b</option><option value="aa:bb:cc:dd:ee:02">host-a</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""

RESTORE_DOSPROTECT_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<select name="flood_protect"><option value="on">On</option><option value="off" selected>Off</option></select>
<input type="submit" name="Save" value="Save"></form></body></html>"""

PACKETFILTER_HTML = (
    """<html><body><form method="post" action="/cgi-bin/packetfilter.ha">"""
    """<input type="submit" name="AddDropRule" value="Add a 'Drop' Rule"></form></body></html>"""
)

ETHERLAN_HTML = """<html><body><form method="post" action="/cgi-bin/etherlan.ha">
<input type="hidden" name="nonce" value="n">
<input type="text" name="lan_mtu" value="1500">
<input type="submit" name="Save" value="Save"></form></body></html>"""

STALE_ROW = (
    '<tr><td>Stale</td><td>1-1</td><td>1</td><td>TCP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>'
)
STALE2_ROW = (
    '<tr><td>Stale2</td><td>2-2</td><td>2</td><td>TCP</td><td><input type="submit" name="Remove_3" value="Remove"></td></tr>'
)

# Rows spliced into RESTORE_SERVICES_HTML by individual restore tests (verbatim from restore.test.ts).
STALE_3_3_REMOVE3_ROW = (
    '<tr><td>Stale</td><td>3-3</td><td>3</td><td>TCP</td><td><input type="submit" name="Remove_3" value="Remove"></td></tr>'
)
STALE2_REMOVE4_ROW = (
    '<tr><td>Stale2</td><td>2-2</td><td>2</td><td>TCP</td><td><input type="submit" name="Remove_4" value="Remove"></td></tr>'
)
ZZ_TEST_SERVICE_ROW = (
    '<tr><td>zz_test</td><td>61000-61000</td><td>61000</td><td>TCP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>'
)

DISABLED_DOSPROTECT_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<select name="flood_protect" disabled><option value="on">On</option><option value="off" selected>Off</option></select>
<input type="submit" name="Save" value="Save"></form></body></html>"""

DOSPROTECT_RENAMED_BUTTON_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<select name="flood_protect"><option value="on">On</option><option value="off" selected>Off</option></select>
<input type="submit" name="Apply" value="Apply"></form></body></html>"""

PORT_STATUS_TABLE = (
    "<table><tr><th>Port</th><th>Status</th><th>Notes</th></tr><tr><td>80</td><td>open</td><td>-</td></tr>"
    "<tr><td>443</td><td>open</td><td>-</td></tr></table>"
)

TCP_NAMED_SERVICE_HTML = """<html><body><form method="post" action="/cgi-bin/services.ha">
<input type="hidden" name="nonce" value="n">
<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>
<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>TCP</td><td>7-7</td><td>7</td><td>UDP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr></table>
<input type="text" name="Service" value=""><input type="text" name="extMinPort" value=""><input type="text" name="extMaxPort" value="">
<input type="text" name="intStartPort" value=""><select name="protocol"><option value="TCP" selected>TCP</option><option value="UDP">UDP</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""

DUP_NAME_HTML = """<html><body><form method="post" action="/cgi-bin/services.ha">
<input type="hidden" name="nonce" value="n">
<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>
<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>Dup</td><td>5-5</td><td>5</td><td>TCP</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>
<tr><td>Dup</td><td>6-6</td><td>6</td><td>TCP</td><td><input type="submit" name="Remove_3" value="Remove"></td></tr></table>
<input type="text" name="Service" value=""><input type="text" name="extMinPort" value=""><input type="text" name="extMaxPort" value="">
<input type="text" name="intStartPort" value=""><select name="protocol"><option value="TCP" selected>TCP</option><option value="UDP">UDP</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""

TWO_FIELD_DOSPROTECT_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<select name="flood_protect" disabled><option value="on">On</option><option value="off" selected>Off</option></select>
<input type="text" name="log_attacks" value="no">
<input type="submit" name="Save" value="Save"></form></body></html>"""

APPHOSTING_EXTRA_HTML = """<html><body><form method="post" action="/cgi-bin/apphosting.ha">
<input type="hidden" name="nonce" value="n">
<table><tr><th>Service</th><th>Needed by Device</th><th></th></tr>
<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>
<tr><td>Mosh</td><td>host-b</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr></table>
<select name="service"><option value="custom_ssh">custom_ssh</option><option value="Mosh">Mosh</option></select>
<select name="device"><option value="aa:bb:cc:dd:ee:01">host-b</option><option value="aa:bb:cc:dd:ee:02">host-a</option></select>
<input type="submit" name="Add" value="Add"></form></body></html>"""

REAL_PROTOCOL_SELECT = (
    '<select name="protocol"><option value="both">TCP/UDP</option><option value="tcp">TCP</option>'
    '<option value="udp">UDP</option></select>'
)

WCONFIG_SAVE_DOTS_HTML = (
    '<html><body><form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="n">'
    '<input type="text" name="maxclients" value="80"><input type="submit" name="Update" value="Update">'
    '<input type="submit" name="Save" value="Save..."></form></body></html>'
)


def entry_page_html(mac: str, ips: Sequence[str]) -> str:
    """TS entryPageHtml(mac, ips): the IP Allocation Entry form the Allocate POST opens."""
    options = "".join(f'<option value="{ip}">Private fixed:{ip}</option>' for ip in ips)
    return (
        '<html><body><form method="post" action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">\n'
        f'<select id="alloc" name="alloc_{mac}" size="8"><option value="normal" selected>Address from DHCP pool</option>'
        f"{options}</select>\n"
        '<input type="submit" name="Save" value="Save"><input type="submit" name="Cancel" value="Cancel"></form></body></html>'
    )


def entry_page_html_with_save(mac: str, ips: Sequence[str], save: str | None) -> str:
    """TS entryPageHtmlWithSave(mac, ips, save): Save button with a rendered value, or none at all."""
    options = "".join(f'<option value="{ip}">Private fixed:{ip}</option>' for ip in ips)
    save_input = "" if save is None else f'<input type="submit" name="Save" value="{save}">'
    return (
        '<html><body><form method="post" action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">\n'
        f'<select id="alloc" name="alloc_{mac}" size="8"><option value="normal" selected>Address from DHCP pool</option>'
        f"{options}</select>\n"
        f'{save_input}<input type="submit" name="Cancel" value="Cancel"></form></body></html>'
    )


def entry_page_html_disabled_option(mac: str) -> str:
    return (
        '<html><body><form method="post" action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="n">\n'
        f'<select id="alloc" name="alloc_{mac}" size="8"><option value="normal" selected>Address from DHCP pool</option>'
        '<option value="192.168.1.67" disabled>Private fixed:192.168.1.67</option></select>\n'
        '<input type="submit" name="Save" value="Save"></form></body></html>'
    )


# The gateway keeps an "IP Allocation Entry" block rendered for the rest of the web session after
# an Allocate/Cancel, so the base ipalloc page can carry a stale `alloc_<other mac>` select.
IPALLOC_STICKY_HTML = IPALLOC_HTML.replace(
    "</form>",
    '<select id="alloc" name="alloc_aa:bb:cc:dd:ee:ff" size="8"><option value="normal" selected>Address from DHCP pool'
    '</option><option value="192.168.1.68">Private fixed:192.168.1.68</option></select>\n'
    '<input type="submit" name="Save" value="Save"></form>',
)

# Real page shape (2026-09-19): Continue posts back to wconfig.ha, Cancel posts to the warning page.
WIFI_WARN_HTML = """<html><head><title>Wi-Fi Warning</title></head><body>
<form method="post" action="/cgi-bin/wconfig.ha"><input type="hidden" name="nonce" value="n"><p>Warning: You have made a change to your Wi-Fi configuration.</p><input type="submit" name="Continue" value="Continue"></form>
<form method="post" action="/cgi-bin/wifiwarn_advanced.ha"><input type="hidden" name="nonce" value="n"><input type="submit" name="Cancel" value="Cancel"></form></body></html>"""

WIFI_WARN_NO_CONTINUE_HTML = (
    '<html><body><form action="/cgi-bin/wifiwarn_advanced.ha"><input type="submit" name="Cancel" value="Cancel">'
    "</form></body></html>"
)

APPHOSTING_WITHOUT_MOSH_HTML = RESTORE_APPHOSTING_HTML.replace('<option value="Mosh">Mosh</option>', "")
APPHOSTING_WITH_STAR_MOSH_HTML = RESTORE_APPHOSTING_HTML.replace(
    '<option value="Mosh">Mosh</option>', '<option value="*Mosh">*Mosh</option>'
)

DOSPROTECT_CHECKBOX_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<input type="checkbox" name="reflexive" value="on" checked>
<input type="checkbox" name="algsip" value="on">
<input type="submit" name="Save" value="Save"></form></body></html>"""

TEXT_FIELD_LITERAL_UNCHECKED_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
<input type="hidden" name="nonce" value="n">
<input type="text" name="display_label" value="Living room">
<input type="checkbox" name="reflexive" value="on" checked>
<input type="submit" name="Save" value="Save"></form></body></html>"""

# --- tests/mutations.test.ts ------------------------------------------------------------------------

I6_MIRROR_HTML = """<html><body><form method="post" action="/cgi-bin/dosprotect.ha">
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

WCONFIG_PARITY_HTML = """<html><body><form method="post" action="/cgi-bin/wconfig.ha">
<input type="hidden" name="nonce" value="abc">
<input type="text" name="maxclients" value="80">
<input type="text" name="ssidname12" value="Guest" disabled>
<input type="text" name="WPSPIN5" value="">
<select name="wl80211on"><option value="on" selected>On</option><option value="off">Off</option></select>
<input type="submit" name="Update" value="Update">
<input type="submit" name="Save" value="Save...">
</form></body></html>"""

# --- Dump-file literals -----------------------------------------------------------------------------

# Produced by the TypeScript CLI itself (bun, BGW320-CLI @ f8a6d5a): parsePage on the eight page
# constants above (SYSINFO/SERVICES/APPHOSTING/IPALLOC/PACKETFILTER/DOSPROTECT/WCONFIG/ETHERLAN) ->
# extractSnapshot(ts="2026-09-19T00:00:00.000Z", routerHost="192.168.1.254") -> writeDumpFile.
# The Python pipeline must reproduce these bytes exactly and load them back to the same Snapshot.
TS_DUMP_JSON = """{
  "meta": {
    "schema": 2,
    "firmware": "4.27.7",
    "ts": "2026-09-19T00:00:00.000Z",
    "routerHost": "192.168.1.254"
  },
  "services": [
    {
      "name": "custom_ssh",
      "extMinPort": 2483,
      "extMaxPort": 2483,
      "intStartPort": 22,
      "protocol": "TCP"
    },
    {
      "name": "Mosh",
      "extMinPort": 60001,
      "extMaxPort": 60010,
      "intStartPort": 60001,
      "protocol": "UDP"
    }
  ],
  "forwards": [
    {
      "service": "custom_ssh",
      "deviceLabel": "host-b",
      "deviceMac": "aa:bb:cc:dd:ee:01"
    },
    {
      "service": "Mosh",
      "deviceLabel": "host-a",
      "deviceMac": "aa:bb:cc:dd:ee:02"
    }
  ],
  "reservations": [
    {
      "mac": "02:0a:0b:0c:0d:02",
      "ip": "192.168.1.64"
    },
    {
      "mac": "02:0a:0b:0c:0d:03",
      "ip": "192.168.1.70"
    }
  ],
  "forms": {
    "dosprotect": {
      "reflexive": "on",
      "unchecked_box": "<unchecked>",
      "flood_protect": "on"
    },
    "wconfig": {
      "ssidname11": "EXAMPLE-NET",
      "key11": "topsecret",
      "maxclients": "80",
      "wl80211on": "on"
    },
    "etherlan": {
      "lan_mtu": "1500"
    }
  },
  "tables": {
    "packetfilter": [
      {
        "button": "Add a 'Drop' Rule"
      }
    ],
    "ipalloc": [
      {
        "IPv4 Address / Name": "192.168.1.64",
        "MAC Address": "02:0A:0B:0C:0D:02",
        "Status": "on",
        "Allocation": "Fixed Allocation",
        "Action": ""
      },
      {
        "IPv4 Address / Name": "192.168.1.70",
        "MAC Address": "02:0a:0b:0c:0d:03",
        "Status": "on",
        "Allocation": "Fixed Allocation",
        "Action": ""
      }
    ],
    "etherlan": [
      {
        "Port": "1",
        "Configured media": "",
        "Configured MDI-X": "",
        "Supported media modes": "",
        "Supported MDI-X modes": ""
      },
      {
        "Port": "2",
        "Configured media": "",
        "Configured MDI-X": "",
        "Supported media modes": "",
        "Supported MDI-X modes": ""
      },
      {
        "Port": "3",
        "Configured media": "",
        "Configured MDI-X": "",
        "Supported media modes": "",
        "Supported MDI-X modes": ""
      },
      {
        "Port": "4",
        "Configured media": "",
        "Configured MDI-X": "",
        "Supported media modes": "",
        "Supported MDI-X modes": ""
      }
    ]
  }
}
"""

# tests/dumpfile.test.ts `goodEntry`, serialized as the TS test wrote it (JSON.stringify, compact).
TS_GOOD_ENTRY_JSON = (
    '{"meta":{"schema":2,"firmware":"6.35.8","ts":"2026-09-20T00:00:00.000Z","routerHost":"192.168.1.254"},'
    '"services":[{"name":"Mosh","extMinPort":60001,"extMaxPort":60010,"intStartPort":60001,"protocol":"UDP"}],'
    '"forwards":[{"service":"Mosh","deviceLabel":"host-a","deviceMac":"02:0a:0b:0c:0d:01"}],'
    '"reservations":[{"mac":"02:0a:0b:0c:0d:01","ip":"192.168.1.65"}],'
    '"forms":{"dosprotect":{"reflexive":"on"}},'
    '"tables":{"ipalloc":[{"MAC Address":"02:0a:0b:0c:0d:01"}]}}'
)
