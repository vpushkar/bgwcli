"""Ported from tests/snapshot-diff.test.ts."""

from dataclasses import replace

from bgwcli.restore import restore_converged
from bgwcli.snapshot import Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService
from bgwcli.snapshot_diff import (
    EntryDiff,
    FormFieldDiff,
    ReservationChange,
    ReservationDiff,
    compared_form_pages,
    diff_snapshots,
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


def test_compared_form_pages_skips_etherlan_unless_include_lan():
    assert compared_form_pages(include_lan=False) == ["dosprotect", "wconfig"]
    assert compared_form_pages(include_lan=True) == ["dosprotect", "wconfig", "etherlan"]


def test_identical_snapshots_produce_an_empty_diff():
    diff = diff_snapshots(snap(), snap(), include_lan=True)
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
    diff = diff_snapshots(dump, live, include_lan=False)
    assert diff.identical is False
    assert [s.name for s in diff.services.missing] == ["Mosh"]
    assert [s.name for s in diff.services.extra] == ["Wireguard"]
    assert [f.device_mac for f in diff.forwards.missing] == ["aa:bb:cc:dd:ee:01"]
    assert diff.forwards.extra == []


def test_form_differences_list_only_changed_fields_and_skip_lan_unless_requested():
    live = snap(forms={"dosprotect": {"flood_protect": "off"}, "etherlan": {"dhcp": "off"}})
    without = diff_snapshots(snap(), live, include_lan=False)
    assert without.forms == {"dosprotect": [FormFieldDiff("flood_protect", "on", "off")]}
    with_lan = diff_snapshots(snap(), live, include_lan=True)
    assert with_lan.forms["etherlan"] == [FormFieldDiff("dhcp", "on", "off")]


def test_form_fields_are_reported_sorted_by_name_including_live_only_fields():
    dump = snap(forms={"dosprotect": {"b": "1", "a": "1"}})
    live = snap(forms={"dosprotect": {"b": "2", "c": "3", "a": "1"}})
    diff = diff_snapshots(dump, live, include_lan=False)
    assert diff.forms == {"dosprotect": [FormFieldDiff("b", "1", "2"), FormFieldDiff("c", None, "3")]}


# The two directions are deliberately asymmetric: a dump that captured a page but finds it gone
# from the router is real drift, while a dump that never captured a page cannot claim anything
# about it.
def test_a_form_page_missing_on_one_side_reports_every_field_only_when_the_dump_has_the_page():
    live_missing = diff_snapshots(snap(), snap(forms={}), include_lan=False)
    assert live_missing.forms["dosprotect"] == [FormFieldDiff("flood_protect", "on", None)]
    dump_missing = diff_snapshots(snap(forms={}), snap(), include_lan=False)
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
    diff = diff_snapshots(dump, live, include_lan=True)
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
    assert diff_snapshots(replace(dump, reservations=live_reservations), live, include_lan=True).identical is True


def test_firmware_change_is_reported_but_does_not_break_identity():
    live = snap(meta=SnapshotMeta(schema=2, firmware="4.28.0", ts="x", router_host="r"))
    diff = diff_snapshots(snap(), live, include_lan=True)
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
    diff = diff_snapshots(dump, live, include_lan=False)
    assert diff.identical is False
    assert diff.reservations.missing == [SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70")]
    assert diff.reservations.changed == [ReservationChange("02:0a:0b:0c:0d:02", "192.168.1.64", "192.168.1.99")]
    assert diff.reservations.extra == [SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65")]


def test_identical_reservations_keep_the_diff_identical():
    diff = diff_snapshots(snap(), snap(), include_lan=True)
    assert diff.reservations == ReservationDiff(missing=[], changed=[], extra=[])
    assert diff.identical is True
