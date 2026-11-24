"""Reset detection needs corroboration for a thin signal (2026-11-24 review):

- services are matched by name: an edited port or protocol is drift, not a missing service;
- a section signal resting on a single dumped entry counts only with a second signal: every dumped
  form page differs (2+ pages), or the fallback access code was needed;
- a dump without entries is a reset when every dumped form page differs, or when the fallback code
  was needed while anything restorable differs.
"""

from bgwcli.autorestore import detect_factory_reset
from bgwcli.snapshot import Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService
from bgwcli.snapshot_diff import diff_snapshots

M = SnapshotMeta("6.35.8", "", "router.local")
SSH = SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")
NAS = SnapshotForward("HTTP Server", "nas", "aa:bb:cc:dd:ee:ff")
RES = SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64")
FORMS = {"dosprotect": {"flood_protect": "on"}, "wconfig": {"ssid": "Old", "key11": "oldpw"}}
RESET_FORMS = {"dosprotect": {"flood_protect": "off"}, "wconfig": {"ssid": "ATTxyz", "key11": "sticker"}}


def detect(dump: Snapshot, live: Snapshot, **kwargs):
    return detect_factory_reset(diff_snapshots(dump, live), dump, **kwargs)


def test_deleting_the_only_forward_and_changing_the_wifi_password_is_drift():
    dump = Snapshot(M, forwards=[NAS], forms={"wconfig": {"ssid": "Old", "key11": "oldpw"}})
    live = Snapshot(M, forwards=[], forms={"wconfig": {"ssid": "Old", "key11": "newpw"}})
    detected, reason = detect(dump, live)
    assert detected is False and "not corroborated" in reason


def test_deleting_the_only_forward_with_both_form_pages_unchanged_is_drift():
    dump = Snapshot(M, forwards=[NAS], forms=FORMS)
    live = Snapshot(M, forwards=[], forms=FORMS)
    assert detect(dump, live)[0] is False


def test_a_port_change_of_the_only_custom_service_is_not_a_missing_service():
    dump = Snapshot(M, services=[SSH], forms=FORMS)
    live = Snapshot(M, services=[SnapshotService("custom_ssh", 2222, 2222, 22, "TCP")], forms=FORMS)
    detected, reason = detect(dump, live)
    assert detected is False and "services 0/1 missing" in reason


def test_a_renamed_service_is_still_missing_by_name():
    dump = Snapshot(M, services=[SSH, SnapshotService("Mosh", 60001, 60010, 60001, "UDP")], forms=FORMS)
    live = Snapshot(M, services=[SnapshotService("ssh2", 2483, 2483, 22, "TCP")], forms=FORMS)
    detected, reason = detect(dump, live)
    assert detected is True and "services 2/2 missing" in reason


def test_every_entry_gone_across_two_or_more_entries_stands_alone():
    dump = Snapshot(M, services=[SSH], reservations=[RES], forms=FORMS)
    live = Snapshot(M, forms=FORMS)
    detected, reason = detect(dump, live)
    assert detected is True and "corroborated" not in reason


def test_a_thin_signal_is_corroborated_by_the_fallback_access_code():
    dump = Snapshot(M, forwards=[NAS], forms=FORMS)
    live = Snapshot(M, forms=FORMS)
    detected, reason = detect(dump, live, used_fallback_code=True)
    assert detected is True and "corroborated by the fallback access code" in reason


def test_a_thin_signal_is_corroborated_when_every_form_page_differs():
    dump = Snapshot(M, forwards=[NAS], forms=FORMS)
    live = Snapshot(M, forms=RESET_FORMS)
    detected, reason = detect(dump, live)
    assert detected is True and "corroborated by 2/2 form pages differ" in reason


def test_one_form_page_never_corroborates_a_thin_signal():
    dump = Snapshot(M, forwards=[NAS], forms={"wconfig": {"ssid": "Old"}})
    live = Snapshot(M, forms={"wconfig": {"ssid": "ATTxyz"}})
    assert detect(dump, live)[0] is False


def test_wifi_only_dump_is_a_reset_when_the_fallback_code_was_needed():
    # Baseline with Firewall Advanced at factory defaults: after a reset only Wi-Fi differs.
    dump = Snapshot(M, forms={"dosprotect": {"dos": "on"}, "wconfig": {"ssid": "MyHome", "key11": "mine"}})
    live = Snapshot(M, forms={"dosprotect": {"dos": "on"}, "wconfig": {"ssid": "ATTxyz", "key11": "sticker"}})
    assert detect(dump, live) == (False, "1/2 form pages differ")
    detected, reason = detect(dump, live, used_fallback_code=True)
    assert detected is True and "corroborated by the fallback access code" in reason


def test_the_fallback_code_alone_never_resets_a_dump_with_entries_partly_present():
    dump = Snapshot(M, services=[SSH], reservations=[RES], forms=FORMS)
    live = Snapshot(M, services=[SSH], forms=FORMS)
    assert detect(dump, live, used_fallback_code=True)[0] is False
