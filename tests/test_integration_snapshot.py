"""Integration: real parser -> snapshot -> diff -> dump file.

Re-derives every tests/snapshot.test.ts case that fed inline HTML through parsePage, now through
bgwcli.parser.parse_page, so the parser and snapshot modules are proven to agree on the same markup
the TS suite used. The dump-file round trip is checked against a dump the TypeScript CLI wrote.
"""

from __future__ import annotations

import json

import pytest
from integration_html import (
    APPHOSTING_HTML,
    DOSPROTECT_HTML,
    ETHERLAN_HTML,
    IPALLOC_HTML,
    PACKETFILTER_HTML,
    RADIO_GROUP_HTML,
    SENTINEL_COLLISION_HTML,
    SERVICES_HTML,
    SYSINFO_HTML,
    TS_DUMP_JSON,
    TS_GOOD_ENTRY_JSON,
    WCONFIG_HTML,
    header_only,
)

from bgwcli.dumpfile import dump_json_text, read_dump_file, write_dump_file
from bgwcli.errors import SnapshotExtractionError
from bgwcli.parser import parse_page
from bgwcli.snapshot import (
    FORM_PAGES,
    OPTIONAL_FORM_PAGES,
    SNAPSHOT_PAGES,
    UNCHECKED,
    SnapshotForward,
    SnapshotReservation,
    SnapshotService,
    extract_snapshot,
    forward_key,
    reservation_key,
    service_key,
)
from bgwcli.snapshot_diff import diff_snapshots
from bgwcli.types import ParsedPage

TS = "2026-09-19T00:00:00.000Z"
ROUTER_HOST = "192.168.1.254"

BASE_HTML = {
    "services": SERVICES_HTML,
    "apphosting": APPHOSTING_HTML,
    "dosprotect": DOSPROTECT_HTML,
    "sysinfo": SYSINFO_HTML,
}


def pages(**overrides: str) -> dict[str, ParsedPage]:
    """TS `pages(overrides)`: parse the base four pages plus overrides with includeSecrets."""
    html = {**BASE_HTML, **overrides}
    return {page: parse_page(page, body, include_secrets=True) for page, body in html.items()}


def snapshot(**overrides: str):
    return extract_snapshot(pages(**overrides), ts=TS, router_host=ROUTER_HOST)


def test_snapshot_pages_and_form_pages_include_wconfig_and_ipalloc():
    assert list(SNAPSHOT_PAGES) == [
        "sysinfo",
        "services",
        "apphosting",
        "ipalloc",
        "packetfilter",
        "dosprotect",
        "wconfig",
        "wconfig_unified",
        "etherlan",
        "dhcpserver",
        "ippass",
        "wmacauth",
    ]
    assert list(FORM_PAGES) == ["dosprotect", "wconfig", "etherlan", "dhcpserver", "ippass", "wmacauth"]


def test_parser_output_matches_the_shape_the_snapshot_module_relies_on():
    # The parser drops the empty trailing <th></th> column and keeps a named empty "Action" column,
    # exactly what page_builders assumed and what extract_snapshot keys its column search on.
    parsed = pages(ipalloc=IPALLOC_HTML)
    assert list(parsed["services"].tables[0]) == ["Service Name", "Global Port Range", "Base Host Port", "Protocol"]
    assert list(parsed["apphosting"].tables[0]) == ["Service", "Needed by Device"]
    assert list(parsed["ipalloc"].tables[0]) == ["IPv4 Address / Name", "MAC Address", "Status", "Allocation", "Action"]
    assert [b.name for b in parsed["services"].buttons] == ["Remove_1", "Remove_2", "Add"]
    assert parsed["sysinfo"].values == {"Software Version": "4.27.7"}
    # The button's label is its value; the select's value is the selected option.
    assert parsed["services"].buttons[0].label == "Remove"
    assert parsed["services"].selects[0].value == "TCP"


def test_extract_snapshot_parses_custom_services_from_a_port_range_table():
    snap = snapshot()
    assert snap.services == [
        SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
        SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
    ]
    assert service_key(snap.services[0]) == "custom_ssh|tcp|2483-2483|22"


def test_extract_snapshot_resolves_forward_device_labels_to_macs_from_the_device_select():
    snap = snapshot()
    assert snap.forwards == [
        SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01"),
        SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"),
    ]
    assert forward_key(snap.forwards[0]) == "custom_ssh|aa:bb:cc:dd:ee:01"


def test_extract_snapshot_refuses_ambiguous_device_labels():
    ambiguous = APPHOSTING_HTML.replace("<td>host-b</td>", "<td>watch</td>")
    with pytest.raises(SnapshotExtractionError, match="ambiguous"):
        snapshot(apphosting=ambiguous)


def test_extract_snapshot_refuses_unknown_device_labels():
    unknown = APPHOSTING_HTML.replace("<td>host-b</td>", "<td>ghost</td>")
    with pytest.raises(SnapshotExtractionError, match="not in the router's device list") as info:
        snapshot(apphosting=unknown)
    assert "re-run dump" in str(info.value)


def test_extract_snapshot_captures_form_values_without_disabled_controls_or_nonce_recording_unchecked():
    snap = snapshot()
    assert snap.forms["dosprotect"] == {"flood_protect": "on", "reflexive": "on", "unchecked_box": UNCHECKED}
    assert UNCHECKED == "<unchecked>"


def test_extract_snapshot_keeps_the_checked_radio_of_a_group():
    snap = snapshot(dosprotect=RADIO_GROUP_HTML)
    assert snap.forms["dosprotect"] == {"mode": "b", "other": UNCHECKED}


def test_extract_snapshot_records_firmware_and_meta():
    meta = snapshot().meta
    assert (meta.schema, meta.firmware, meta.ts, meta.router_host) == (2, "4.27.7", TS, ROUTER_HOST)


def test_extract_snapshot_tolerates_missing_pages():
    snap = extract_snapshot({}, ts=TS, router_host=ROUTER_HOST)
    assert snap.services == [] and snap.forwards == [] and snap.reservations == []
    assert snap.forms == {}
    assert snap.meta.firmware == ""


def test_extract_snapshot_fails_loudly_when_a_services_table_lacks_a_port_column():
    broken = SERVICES_HTML.replace("<th>Global Port Range</th>", "<th>Mystery</th>")
    with pytest.raises(SnapshotExtractionError, match="column"):
        snapshot(services=broken)


def test_extract_snapshot_records_fixed_ip_allocations_as_reservations_with_lowercased_macs():
    snap = snapshot(ipalloc=IPALLOC_HTML)
    assert snap.meta.schema == 2
    assert snap.reservations == [
        SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
        SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70"),
    ]
    assert reservation_key(snap.reservations[0]) == "02:0a:0b:0c:0d:02"
    assert len(snap.tables["ipalloc"]) == 2  # fixed rows only by default


def test_extract_snapshot_fails_loudly_on_a_fixed_row_without_an_ipv4_address():
    broken = IPALLOC_HTML.replace("<td>192.168.1.70</td>", "<td>printer</td>")
    with pytest.raises(SnapshotExtractionError, match="no IPv4 address"):
        snapshot(ipalloc=broken)


def test_extract_snapshot_captures_advanced_wifi_form_values_without_disabled_or_wps_pin_fields():
    snap = snapshot(wconfig=WCONFIG_HTML)
    assert snap.forms["wconfig"] == {
        "ssidname11": "EXAMPLE-NET",
        "key11": "topsecret",
        "maxclients": "80",
        "wl80211on": "on",
    }
    assert "wconfig_unified" not in snap.forms


def test_wconfig_page_specific_tables_do_not_leak_into_the_snapshot():
    # parse_page synthesizes Advanced Wi-Fi summary rows for wconfig (page_builders never modelled
    # them); the snapshot only records documentary tables, so they must stay out of the dump.
    parsed = parse_page("wconfig", WCONFIG_HTML, include_secrets=True)
    assert parsed.tables and parsed.tables[0].get("Section") == "Radio"
    assert "wconfig" not in snapshot(wconfig=WCONFIG_HTML).tables


@pytest.mark.parametrize(
    ("page", "html", "old", "pattern"),
    [
        ("services", SERVICES_HTML, "<th>Service Name</th>", r"services: .*name.*headers were"),
        ("apphosting", APPHOSTING_HTML, "<th>Service</th>", r"apphosting: .*headers were"),
        ("ipalloc", IPALLOC_HTML, "<th>Allocation</th>", r"ipalloc: .*headers were"),
    ],
)
def test_extract_snapshot_fails_loudly_when_no_row_carries_a_recognizable_identifying_column(page, html, old, pattern):
    # A firmware update that renames an identifying column must not turn a populated table into an
    # empty section with exit 0 (every dumped entry would look "missing", every live one "extra").
    with pytest.raises(SnapshotExtractionError, match=pattern):
        snapshot(**{page: html.replace(old, "<th>Mystery</th>")})


def test_extract_snapshot_still_returns_empty_sections_for_header_only_tables():
    snap = snapshot(services=header_only(SERVICES_HTML), apphosting=header_only(APPHOSTING_HTML))
    assert snap.services == []
    assert snap.forwards == []


def test_extract_snapshot_refuses_a_checkable_whose_submit_value_collides_with_the_sentinel():
    # The parser decodes &lt;unchecked&gt; to the literal sentinel, and the snapshot refuses it in
    # both states: the collision is a property of the control, not of its current state.
    parsed = parse_page("dosprotect", SENTINEL_COLLISION_HTML, include_secrets=True)
    assert [f.value for f in parsed.fields] == [UNCHECKED]
    with pytest.raises(SnapshotExtractionError, match=r"dosprotect: .*weird.*collides"):
        snapshot(dosprotect=SENTINEL_COLLISION_HTML)
    with pytest.raises(SnapshotExtractionError, match="collides"):
        snapshot(dosprotect=SENTINEL_COLLISION_HTML.replace(" checked", ""))


def test_parser_redaction_flows_into_the_snapshot_so_dump_must_parse_with_include_secrets():
    # The dump command parses with include_secrets=True; anything else would put redacted values
    # in the "truth" restore writes back. Pin the coupling from the parser's redaction.
    redacted = extract_snapshot({"wconfig": parse_page("wconfig", WCONFIG_HTML)}, ts=TS, router_host=ROUTER_HOST)
    assert redacted.forms["wconfig"]["key11"] != "topsecret"
    assert redacted.forms["wconfig"]["ssidname11"] == "EXAMPLE-NET"


# --- dump file round trip against the TypeScript CLI's own output -----------------------------------


def full_pages() -> dict[str, ParsedPage]:
    html = {
        "sysinfo": SYSINFO_HTML,
        "services": SERVICES_HTML,
        "apphosting": APPHOSTING_HTML,
        "ipalloc": IPALLOC_HTML,
        "packetfilter": PACKETFILTER_HTML,
        "dosprotect": DOSPROTECT_HTML,
        "wconfig": WCONFIG_HTML,
        "etherlan": ETHERLAN_HTML,
    }
    return {page: parse_page(page, body, include_secrets=True) for page, body in html.items()}


# The TypeScript CLI always captured etherlan; the Python dump only does with `--include etherlan`
# (or `all`), which is what these byte-identity tests model.
def test_dump_json_from_the_real_parser_is_byte_identical_to_the_typescript_dump():
    snap = extract_snapshot(full_pages(), ts=TS, router_host=ROUTER_HOST, include=("etherlan",))
    assert dump_json_text(snap) == TS_DUMP_JSON


def test_dump_file_round_trip_write_read_diff_identical(tmp_env):
    snap = extract_snapshot(full_pages(), ts=TS, router_host=ROUTER_HOST, include=OPTIONAL_FORM_PAGES)
    path = tmp_env / "dumps" / "nested" / "bgw-dump.json"
    write_dump_file(path, snap)
    assert path.read_text(encoding="utf-8") == TS_DUMP_JSON
    loaded = read_dump_file(path)
    assert loaded == snap
    assert diff_snapshots(loaded, snap).identical is True
    assert diff_snapshots(snap, loaded).identical is True


def test_a_dump_written_by_the_typescript_cli_loads_and_diffs_clean_against_a_live_python_snapshot(tmp_env):
    path = tmp_env / "ts-dump.json"
    path.write_text(TS_DUMP_JSON, encoding="utf-8")
    dump = read_dump_file(path)
    live = extract_snapshot(full_pages(), ts="later", router_host=ROUTER_HOST, include=OPTIONAL_FORM_PAGES)
    diff = diff_snapshots(dump, live)
    assert diff.identical is True
    assert diff.firmware_changed is False
    # Ordering inside forms is also the TS order (fields, then unchecked, then selects).
    assert list(dump.forms["dosprotect"]) == ["reflexive", "unchecked_box", "flood_protect"]


def test_the_typescript_good_entry_literal_loads_byte_compatibly(tmp_env):
    path = tmp_env / "good.json"
    path.write_text(TS_GOOD_ENTRY_JSON, encoding="utf-8")
    loaded = read_dump_file(path)
    assert loaded.meta.firmware == "6.35.8"
    assert loaded.services == [SnapshotService("Mosh", 60001, 60010, 60001, "UDP")]
    assert loaded.forwards == [SnapshotForward("Mosh", "host-a", "02:0a:0b:0c:0d:01")]
    assert loaded.reservations == [SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65")]
    assert loaded.forms == {"dosprotect": {"reflexive": "on"}}
    assert loaded.tables == {"ipalloc": [{"MAC Address": "02:0a:0b:0c:0d:01"}]}
    # Re-serialising yields the same document JSON.stringify would (pretty layout differs only in whitespace).
    assert json.loads(dump_json_text(loaded)) == json.loads(TS_GOOD_ENTRY_JSON)


def test_diff_between_the_snapshot_pages_and_the_restore_pages_is_real_drift_not_parser_noise():
    # Two page sets that differ only in their rows must diff on exactly those rows.
    from integration_html import RESTORE_APPHOSTING_HTML, RESTORE_SERVICES_HTML

    dump = snapshot()
    live = extract_snapshot(
        {
            "services": parse_page("services", RESTORE_SERVICES_HTML, include_secrets=True),
            "apphosting": parse_page("apphosting", RESTORE_APPHOSTING_HTML, include_secrets=True),
        },
        ts=TS,
        router_host=ROUTER_HOST,
    )
    diff = diff_snapshots(dump, live)
    assert [s.name for s in diff.services.missing] == ["Mosh"]
    assert [s.name for s in diff.services.extra] == ["Stale"]
    assert [f.service for f in diff.forwards.missing] == ["Mosh"]
    assert diff.forwards.extra == []
    # dosprotect is in the dump but not fetched live: reported field by field with live=None.
    assert [(c.field, c.dump, c.live) for c in diff.forms["dosprotect"]] == [
        ("flood_protect", "on", None),
        ("reflexive", "on", None),
        ("unchecked_box", UNCHECKED, None),
    ]
