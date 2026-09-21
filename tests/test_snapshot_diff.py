"""Ported from tests/snapshot-diff.test.ts."""

from dataclasses import replace

import pytest

from bgwcli.errors import UsageError
from bgwcli.restore import restore_converged
from bgwcli.snapshot import FORM_PAGES, Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService
from bgwcli.snapshot_diff import (
    RESTORABLE_PAGES,
    EntryDiff,
    FormFieldDiff,
    ReservationChange,
    ReservationDiff,
    diff_snapshots,
    pages_missing_from_dump,
    resolve_include,
)


def snap(**overrides) -> Snapshot:
    base = Snapshot(
        meta=SnapshotMeta(schema=2, firmware="4.27.7", ts="2026-09-19T00:00:00.000Z", router_host="r"),
        services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")],
        forwards=[SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01")],
        reservations=[SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64")],
        forms={"dosprotect": {"flood_protect": "on"}, "etherlan": {"dhcp": "on"}},
        tables={},
    )
    return replace(base, **overrides)


def test_restorable_pages_are_the_three_sections_plus_every_form_page():
    assert ("services", "apphosting", "ipalloc", *FORM_PAGES) == RESTORABLE_PAGES


def test_resolve_include_for_diff_and_restore():
    assert resolve_include(None) is None
    assert resolve_include("") is None
    assert resolve_include([]) is None
    assert resolve_include("all") is None and resolve_include(["ALL"]) is None
    assert resolve_include("dhcpserver,services") == ("services", "dhcpserver")  # RESTORABLE_PAGES order
    assert resolve_include(["ipalloc", "wconfig", "ipalloc"]) == ("ipalloc", "wconfig")
    with pytest.raises(UsageError) as info:
        resolve_include("services,packetfilter")
    assert "packetfilter" in str(info.value) and "dhcpserver" in str(info.value)


def test_pages_missing_from_dump_only_reports_form_pages_absent_from_forms():
    dump = snap()  # forms: dosprotect, etherlan
    assert pages_missing_from_dump(dump, ("services", "apphosting", "ipalloc", "dosprotect", "etherlan")) == []
    assert pages_missing_from_dump(dump, ("dhcpserver", "wconfig", "etherlan")) == ["wconfig", "dhcpserver"]
    assert pages_missing_from_dump(dump, None) == []


def test_identical_snapshots_produce_an_empty_diff():
    diff = diff_snapshots(snap(), snap())
    assert diff.identical is True
    assert diff.services == EntryDiff(missing=[], extra=[])
    assert diff.forwards == EntryDiff(missing=[], extra=[])
    assert diff.reservations == ReservationDiff(missing=[], changed=[], extra=[])
    assert diff.forms == {}
    assert diff.firmware_changed is False


def test_services_and_forwards_are_compared_by_content_order_insensitive():
    dump = snap(
        services=[
            SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
            SnapshotService("custom_ssh", 2483, 2483, 22, "tcp"),
        ]
    )
    live = snap(
        services=[
            SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
            SnapshotService("Wireguard", 51820, 51820, 51820, "UDP"),
        ],
        forwards=[],
    )
    diff = diff_snapshots(dump, live)
    assert diff.identical is False
    assert [s.name for s in diff.services.missing] == ["Mosh"]
    assert [s.name for s in diff.services.extra] == ["Wireguard"]
    assert [f.device_mac for f in diff.forwards.missing] == ["aa:bb:cc:dd:ee:01"]
    assert diff.forwards.extra == []


def test_form_differences_list_only_changed_fields_for_every_page_the_dump_captured():
    live = snap(forms={"dosprotect": {"flood_protect": "off"}, "etherlan": {"dhcp": "off"}})
    diff = diff_snapshots(snap(), live)
    assert diff.forms == {
        "dosprotect": [FormFieldDiff("flood_protect", "on", "off")],
        "etherlan": [FormFieldDiff("dhcp", "on", "off")],
    }


def test_pages_restricts_the_diff_to_the_selected_sections_and_form_pages():
    live = snap(
        services=[],
        forwards=[],
        reservations=[],
        forms={"dosprotect": {"flood_protect": "off"}, "etherlan": {"dhcp": "off"}},
    )
    everything = diff_snapshots(snap(), live)
    assert everything.identical is False
    assert set(everything.forms) == {"dosprotect", "etherlan"}

    only_lan = diff_snapshots(snap(), live, pages=("etherlan",))
    assert only_lan.forms == {"etherlan": [FormFieldDiff("dhcp", "on", "off")]}
    assert only_lan.services == EntryDiff() and only_lan.forwards == EntryDiff()
    assert only_lan.reservations == ReservationDiff()
    assert only_lan.identical is False

    only_services = diff_snapshots(snap(), live, pages=("services",))
    assert [s.name for s in only_services.services.missing] == ["custom_ssh"]
    assert only_services.forwards == EntryDiff() and only_services.reservations == ReservationDiff()
    assert only_services.forms == {}

    only_forwards = diff_snapshots(snap(), live, pages=("apphosting",))
    assert [f.service for f in only_forwards.forwards.missing] == ["custom_ssh"]
    assert only_forwards.services == EntryDiff()

    only_reservations = diff_snapshots(snap(), live, pages=("ipalloc",))
    assert only_reservations.reservations.missing == snap().reservations
    assert only_reservations.services == EntryDiff() and only_reservations.forms == {}

    # A page the dump never captured is still skipped even when explicitly requested.
    requested_but_absent = diff_snapshots(snap(), live, pages=("wconfig",))
    assert requested_but_absent.identical is True and requested_but_absent.forms == {}
    # Firmware is compared regardless of the selection.
    assert diff_snapshots(snap(), snap(), pages=("services",)).firmware_changed is False


def test_form_fields_are_reported_sorted_by_name_including_live_only_fields():
    dump = snap(forms={"dosprotect": {"b": "1", "a": "1"}})
    live = snap(forms={"dosprotect": {"b": "2", "c": "3", "a": "1"}})
    diff = diff_snapshots(dump, live)
    assert diff.forms == {"dosprotect": [FormFieldDiff("b", "1", "2"), FormFieldDiff("c", None, "3")]}


# The two directions are deliberately asymmetric: a dump that captured a page but finds it gone
# from the router is real drift, while a dump that never captured a page cannot claim anything
# about it.
def test_a_form_page_missing_on_one_side_reports_every_field_only_when_the_dump_has_the_page():
    live_missing = diff_snapshots(snap(), snap(forms={}))
    assert live_missing.forms["dosprotect"] == [FormFieldDiff("flood_protect", "on", None)]
    dump_missing = diff_snapshots(snap(forms={}), snap())
    assert dump_missing.forms == {}


def test_a_schema_1_shaped_dump_never_claims_the_wconfig_page_it_did_not_capture():
    # Schema-1 shaped: no `forms.wconfig` and no reservations. read_dump_file rejects schema-1
    # files outright, so this shape can only reach the diff via a hand-built object; the rule it
    # exercises (a dump never claims a page it did not capture) still guards real drift on
    # schema 2 when a firmware update drops a form page.
    dump = snap(reservations=[])
    live_reservations = [
        SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
        SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70"),
        SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65"),
        SnapshotReservation("02:0a:0b:0c:0d:04", "192.168.1.67"),
    ]
    live = snap(
        reservations=live_reservations,
        forms={
            "dosprotect": {"flood_protect": "on"},
            "etherlan": {"dhcp": "on"},
            "wconfig": {"maxclients": "80", "ssid": "home"},
        },
    )
    diff = diff_snapshots(dump, live)
    assert diff.forms == {}
    assert diff.services == EntryDiff(missing=[], extra=[])
    assert diff.forwards == EntryDiff(missing=[], extra=[])
    assert diff.reservations.missing == []
    assert diff.reservations.changed == []
    assert diff.reservations.extra == live_reservations
    # Router-only reservations are reported but never restored, so the restore still converges.
    assert restore_converged(diff, prune=False) is True
    # The only thing holding `identical` back is the router-only reservations, not wconfig.
    assert diff.identical is False
    assert diff_snapshots(replace(dump, reservations=live_reservations), live).identical is True


def test_firmware_change_is_reported_but_does_not_break_identity():
    live = snap(meta=SnapshotMeta(schema=2, firmware="4.28.0", ts="x", router_host="r"))
    diff = diff_snapshots(snap(), live)
    assert diff.firmware_changed is True
    assert diff.identical is True


def test_reservations_are_compared_by_mac_missing_changed_ip_extra():
    dump = snap(
        reservations=[
            SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
            SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70"),
        ]
    )
    live = snap(
        reservations=[
            SnapshotReservation("02:0A:0B:0C:0D:02", "192.168.1.99"),
            SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65"),
        ]
    )
    diff = diff_snapshots(dump, live)
    assert diff.identical is False
    assert diff.reservations.missing == [SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70")]
    assert diff.reservations.changed == [ReservationChange("02:0a:0b:0c:0d:02", "192.168.1.64", "192.168.1.99")]
    assert diff.reservations.extra == [SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65")]


def test_identical_reservations_keep_the_diff_identical():
    diff = diff_snapshots(snap(), snap())
    assert diff.reservations == ReservationDiff(missing=[], changed=[], extra=[])
    assert diff.identical is True
