"""Ported from tests/snapshot.test.ts; pages built with page_builders instead of parsed HTML."""

import pytest
from page_builders import (
    apphosting_page,
    button,
    checkbox,
    dosprotect_page,
    field,
    hidden,
    ipalloc_page,
    page,
    radio,
    select,
    services_page,
    sysinfo_page,
    table,
)

from bgwcli.errors import SnapshotExtractionError
from bgwcli.snapshot import (
    FORM_PAGES,
    SNAPSHOT_PAGES,
    UNCHECKED,
    SnapshotForward,
    SnapshotMeta,
    SnapshotReservation,
    SnapshotService,
    extract_snapshot,
    forward_key,
    reservation_key,
    service_key,
)
from bgwcli.types import ParsedPage

TS = "2026-09-19T00:00:00.000Z"
HOST = "192.168.1.254"


def default_dosprotect() -> ParsedPage:
    return dosprotect_page(
        fields=[checkbox("reflexive", "on", checked=True), checkbox("unchecked_box")],
        selects=[
            select("flood_protect", [("on", "On"), ("off", "Off")]),
            select("disabled_thing", ["x"], disabled=True),
        ],
    )


def pages(**overrides: ParsedPage) -> dict[str, ParsedPage]:
    out = {
        "services": services_page(),
        "apphosting": apphosting_page(),
        "dosprotect": default_dosprotect(),
        "sysinfo": sysinfo_page(),
    }
    out.update(overrides)
    return out


def snap(**overrides: ParsedPage):
    return extract_snapshot(pages(**overrides), ts=TS, router_host=HOST)


def test_snapshot_pages_and_form_pages_are_fixed():
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
    # New form pages (2026-09-20): Subnets & DHCP, IP Passthrough, Wi-Fi MAC Filtering modes.
    assert list(FORM_PAGES) == ["dosprotect", "wconfig", "etherlan", "dhcpserver", "ippass", "wmacauth"]


def test_extract_snapshot_parses_custom_services_from_a_port_range_table():
    s = snap()
    assert s.services == [
        SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
        SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
    ]
    assert service_key(s.services[0]) == "custom_ssh|tcp|2483-2483|22"


def test_extract_snapshot_reads_separate_min_max_columns_and_defaults_max_to_min():
    headers = ("Name", "From Port", "To Port", "Map To", "Proto")
    two_cols = page("services", tables=table(headers, ("svc", "10", "20", "30", "udp")))
    s = extract_snapshot({"services": two_cols}, ts=TS, router_host=HOST)
    assert s.services == [SnapshotService("svc", 10, 20, 30, "UDP")]
    no_max = page("services", tables=table(("Name", "Port Start", "Host Port", "Protocol"), ("svc", "10", "30", "TCP")))
    s = extract_snapshot({"services": no_max}, ts=TS, router_host=HOST)
    assert s.services == [SnapshotService("svc", 10, 10, 30, "TCP")]


def test_extract_snapshot_rejects_a_non_numeric_port():
    bad = services_page(rows=[("svc", "abc-10", "22", "TCP")])
    with pytest.raises(SnapshotExtractionError, match="extMinPort 'abc' is not a port number"):
        extract_snapshot({"services": bad}, ts=TS, router_host=HOST)


def test_extract_snapshot_skips_rows_with_an_empty_service_name():
    s = extract_snapshot({"services": services_page(rows=[("", "1-1", "1", "TCP")])}, ts=TS, router_host=HOST)
    assert s.services == []


def test_extract_snapshot_resolves_forward_device_labels_to_macs_from_the_device_select():
    s = snap()
    assert s.forwards == [
        SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01"),
        SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"),
    ]
    assert forward_key(s.forwards[0]) == "custom_ssh|aa:bb:cc:dd:ee:01"


def test_extract_snapshot_falls_back_to_an_all_mac_select_when_no_select_is_named_device():
    parsed = apphosting_page()
    parsed.selects = [select("target", [("AA:BB:CC:DD:EE:01", "host-b"), ("aa:bb:cc:dd:ee:02", "host-a")])]
    s = extract_snapshot({"apphosting": parsed}, ts=TS, router_host=HOST)
    assert s.forwards[0].device_mac == "aa:bb:cc:dd:ee:01"


def test_extract_snapshot_refuses_ambiguous_device_labels():
    ambiguous = apphosting_page(rows=[("custom_ssh", "watch"), ("Mosh", "host-a")])
    with pytest.raises(SnapshotExtractionError, match="ambiguous"):
        snap(apphosting=ambiguous)


def test_extract_snapshot_refuses_unknown_device_labels():
    # Reversed ruling (re-review, round 3): recording an unknown label with an empty MAC made
    # `restore --prune` delete the very forward the dump was meant to preserve, because
    # `service|` never matches `service|<mac>` once the device is back online.
    unknown = apphosting_page(rows=[("custom_ssh", "ghost"), ("Mosh", "host-a")])
    with pytest.raises(SnapshotExtractionError, match="not in the router's device list"):
        snap(apphosting=unknown)
    with pytest.raises(SnapshotExtractionError, match="re-run dump"):
        snap(apphosting=unknown)


def test_extract_snapshot_captures_form_values_recording_unchecked_boxes_as_unchecked():
    # An unchecked box must be recorded, not omitted: an omitted key is indistinguishable from a
    # field the dump never knew about, so restore could never re-uncheck a box the owner had off.
    s = snap()
    assert s.forms["dosprotect"] == {"flood_protect": "on", "reflexive": "on", "unchecked_box": UNCHECKED}
    assert UNCHECKED == "<unchecked>"


def test_extract_snapshot_keeps_the_checked_radio_of_a_group():
    parsed = dosprotect_page(
        fields=[radio("mode", "a"), radio("mode", "b", checked=True), radio("mode", "c"), radio("other", "x")]
    )
    assert snap(dosprotect=parsed).forms["dosprotect"] == {"mode": "b", "other": UNCHECKED}


def test_extract_snapshot_records_firmware_and_meta():
    assert snap().meta == SnapshotMeta(schema=2, firmware="4.27.7", ts=TS, router_host=HOST)


def test_extract_snapshot_reads_firmware_version_alias():
    s = extract_snapshot({"sysinfo": page("sysinfo", values={"Firmware Version": " 9.9 "})}, ts=TS, router_host=HOST)
    assert s.meta.firmware == "9.9"


def test_extract_snapshot_tolerates_missing_pages():
    s = extract_snapshot({}, ts=TS, router_host=HOST)
    assert s.services == []
    assert s.forwards == []
    assert s.reservations == []
    assert s.forms == {}
    assert s.tables == {}
    assert s.meta.firmware == ""


def test_extract_snapshot_fails_loudly_when_a_services_table_lacks_a_port_column():
    broken = services_page(headers=("Service Name", "Mystery", "Base Host Port", "Protocol", ""))
    with pytest.raises(SnapshotExtractionError, match="column"):
        snap(services=broken)


def test_extract_snapshot_records_fixed_ip_allocations_as_reservations_with_lowercased_macs():
    s = snap(ipalloc=ipalloc_page())
    assert s.meta.schema == 2
    assert s.reservations == [
        SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
        SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70"),
    ]
    assert reservation_key(s.reservations[0]) == "02:0a:0b:0c:0d:02"
    assert len(s.tables["ipalloc"]) == 2  # fixed rows only by default


def test_extract_snapshot_fails_loudly_on_a_fixed_row_without_an_ipv4_address():
    broken = ipalloc_page(
        rows=[
            ("192.168.1.64", "02:0A:0B:0C:0D:02", "on", "Fixed Allocation"),
            ("printer", "02:0a:0b:0c:0d:03", "on", "Fixed Allocation"),
        ]
    )
    with pytest.raises(SnapshotExtractionError, match="no IPv4 address"):
        snap(ipalloc=broken)


def test_extract_snapshot_fails_loudly_on_a_fixed_row_with_an_invalid_mac():
    broken = ipalloc_page(rows=[("192.168.1.64", "not-a-mac", "on", "Fixed Allocation")])
    with pytest.raises(SnapshotExtractionError, match="invalid MAC"):
        snap(ipalloc=broken)


def test_extract_snapshot_tolerates_a_missing_ipalloc_page():
    assert extract_snapshot({}, ts=TS, router_host=HOST).reservations == []


def test_extract_snapshot_captures_advanced_wifi_form_values_without_disabled_or_wps_pin_fields():
    wconfig = page(
        "wconfig",
        title="Advanced Wi-Fi",
        fields=[
            hidden("nonce", "abc"),
            field("ssidname11", "text", "EXAMPLE-NET"),
            field("key11", "text", "topsecret"),
            field("maxclients", "text", "80"),
            field("ssidname12", "text", "Guest", disabled=True),
            field("WPSPIN5", "text", ""),
        ],
        selects=[
            select("wl80211on", [("on", "On"), ("off", "Off")]),
            select("gssidisolate", [("on", "On")], disabled=True),
        ],
        buttons=[button("Update"), button("Save", "Save..."), button("Cancel")],
    )
    s = snap(wconfig=wconfig)
    assert s.forms["wconfig"] == {
        "ssidname11": "EXAMPLE-NET",
        "key11": "topsecret",
        "maxclients": "80",
        "wl80211on": "on",
    }
    assert "wconfig_unified" not in s.forms


def test_extract_snapshot_documentary_tables_include_packetfilter_buttons():
    pf = page("packetfilter", buttons=[button("AddDropRule", "Add a 'Drop' Rule"), button("noname", "")])
    s = snap(packetfilter=pf, etherlan=page("etherlan", tables=[{"a": "1", "b": "2", "c": "3"}]))
    assert s.tables["packetfilter"] == [{"button": "Add a 'Drop' Rule"}, {"button": "noname"}]
    assert s.tables["etherlan"] == [{"a": "1", "b": "2", "c": "3"}]
    assert "dosprotect" not in s.tables


# A firmware update that renames an identifying column must not turn a populated table into an
# empty section with exit 0: an empty live snapshot would make every dumped entry look "missing"
# (restore re-adds them all) and an empty dump would make every live entry "extra" (--prune
# removes them all). Rows that lack the column are still tolerated as long as at least one row
# on the page carries it — parse_page flattens unrelated >=3-column tables into the same list.
def test_extract_snapshot_fails_loudly_when_no_services_row_carries_a_recognizable_name_column():
    broken = services_page(headers=("Mystery", "Global Port Range", "Base Host Port", "Protocol", ""))
    with pytest.raises(SnapshotExtractionError, match=r"services: .*name.*headers were"):
        snap(services=broken)


def test_extract_snapshot_fails_loudly_when_no_forwards_row_carries_recognizable_columns():
    broken = apphosting_page(headers=("Mystery", "Needed by Device", ""))
    with pytest.raises(SnapshotExtractionError, match=r"apphosting: .*headers were"):
        snap(apphosting=broken)


def test_extract_snapshot_fails_loudly_when_no_ipalloc_row_carries_a_recognizable_allocation_column():
    broken = ipalloc_page(headers=("IPv4 Address / Name", "MAC Address", "Status", "Mystery", "Action"))
    with pytest.raises(SnapshotExtractionError, match=r"ipalloc: .*headers were"):
        snap(ipalloc=broken)


def test_extract_snapshot_tolerates_unrelated_rows_when_one_row_carries_the_column():
    unrelated = [{"Port": "80", "Status": "open", "Notes": "-"}]
    s = snap(services=services_page(extra_tables=unrelated))
    assert [svc.name for svc in s.services] == ["custom_ssh", "Mosh"]


def test_extract_snapshot_still_returns_empty_sections_for_header_only_tables():
    s = snap(services=services_page(rows=[]), apphosting=apphosting_page(rows=[]))
    assert s.services == []
    assert s.forwards == []


def test_extract_snapshot_refuses_a_checkable_whose_submit_value_collides_with_the_sentinel():
    # Checked, such a control would record "<unchecked>" as its value — identical to the unchecked
    # encoding — so saved-off vs live-on would diff as identical. Fail at capture, where it is visible.
    checked = dosprotect_page(fields=[checkbox("weird", UNCHECKED, checked=True)])
    with pytest.raises(SnapshotExtractionError, match=r"dosprotect: .*weird.*collides"):
        snap(dosprotect=checked)
    # Refused in the unchecked state too: the collision is a property of the control, not of its
    # current state, and a dump must never depend on which state the page happened to be in.
    unchecked = dosprotect_page(fields=[checkbox("weird", UNCHECKED)])
    with pytest.raises(SnapshotExtractionError, match="collides"):
        snap(dosprotect=unchecked)


def test_a_text_field_whose_value_is_the_sentinel_is_recorded_as_is():
    parsed = dosprotect_page(fields=[field("label", "text", UNCHECKED)])
    assert snap(dosprotect=parsed).forms["dosprotect"] == {"label": UNCHECKED}


def test_ipalloc_table_defaults_to_fixed_rows_and_all_clients_keeps_dhcp_rows():
    """Default dump trims the documentary tables.ipalloc block to Fixed Allocation rows (no trace of
    DHCP-only devices); `all_clients=True` keeps every row. `reservations` is unaffected either way."""
    from page_builders import ipalloc_page

    pages = {"ipalloc": ipalloc_page()}
    default = extract_snapshot(pages, ts="t", router_host="r")
    everything = extract_snapshot(pages, ts="t", router_host="r", all_clients=True)
    assert 0 < len(default.tables["ipalloc"]) < len(everything.tables["ipalloc"])
    assert all("fixed" in row["Allocation"].lower() for row in default.tables["ipalloc"])
    assert any("dhcp" in row["Allocation"].lower() for row in everything.tables["ipalloc"])
    assert default.reservations == everything.reservations


def test_wmacauth_filter_list_is_documentary_and_its_modes_are_form_fields():
    """MAC Filtering: the allow/deny/none mode selects restore as form fields (the gateway enables
    them only once the MAC list is non-empty; disabled ones are skipped like any disabled control).
    The add-a-MAC sub-form (macaddress, maclist helper dropdown, ssid* checkboxes) is NOT
    configuration, and the filter list itself (Radio/Network/Filtering rows) is recorded as a
    documentary table, not restored."""
    from page_builders import field, hidden, page, select

    wm = page(
        "wmacauth",
        title="Wi-Fi MAC Filtering",
        fields=[
            hidden("nonce", "n"),
            field("macaddress", "text", ""),
            field("ssid11", "checkbox", "", checked=False),
        ],
        selects=[
            select("wmacr1user", ["allow", "deny", "none"], selected="none"),
            select("wmacr2user", ["allow", "deny", "none"], selected="deny"),
            select("wmacr1guest", ["allow", "deny", "none"], selected="none", disabled=True),
            select("maclist", ["Select from this list", "aa:bb:cc:dd:ee:01"], selected="Select from this list"),
        ],
        tables=[{"Radio": "2.4 GHz", "Network": "Home", "Filtering": "none"}],
    )
    s = extract_snapshot({"wmacauth": wm}, ts="t", router_host="r")
    assert s.forms["wmacauth"] == {"wmacr1user": "none", "wmacr2user": "deny"}
    assert s.tables["wmacauth"] == [{"Radio": "2.4 GHz", "Network": "Home", "Filtering": "none"}]
