"""Ported from tests/dumpfile.test.ts, plus byte-compatibility checks against the TS JSON layout."""

import json
import os
import re
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bgwcli.dumpfile import (
    default_dump_path,
    dump_json_text,
    read_dump_file,
    snapshot_from_dict,
    snapshot_to_dict,
    write_dump_file,
)
from bgwcli.errors import DumpFileError
from bgwcli.snapshot import Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService

SNAPSHOT = Snapshot(
    meta=SnapshotMeta(schema=2, firmware="4.27.7", ts="2026-09-19T00:00:00.000Z", router_host="r"),
    services=[],
    forwards=[],
    reservations=[],
    forms={"wconfig_unified": {"wpa_key": "super-secret"}},
    tables={},
)

GOOD = Snapshot(
    meta=SnapshotMeta(schema=2, firmware="6.35.8", ts="2026-09-20T00:00:00.000Z", router_host="192.168.1.254"),
    services=[SnapshotService("Mosh", 60001, 60010, 60001, "UDP")],
    forwards=[SnapshotForward("Mosh", "host-a", "02:0a:0b:0c:0d:01")],
    reservations=[SnapshotReservation("02:0a:0b:0c:0d:01", "192.168.1.65")],
    forms={"dosprotect": {"reflexive": "on"}},
    tables={"ipalloc": [{"MAC Address": "02:0a:0b:0c:0d:01"}]},
)


def test_default_dump_path_uses_bgw_dump_dir_and_a_timestamped_name(monkeypatch):
    monkeypatch.setenv("BGW_DUMP_DIR", "/tmp/bgw-dumps-test")
    when = datetime(2026, 9, 19, 7, 5, 9, tzinfo=timezone.utc)
    assert str(default_dump_path(when)) == "/tmp/bgw-dumps-test/bgw-dump-20260919-070509.json"


def test_default_dump_path_falls_back_to_xdg_state_home_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BGW_DUMP_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert default_dump_path(when) == tmp_path / "state" / "bgw" / "dumps" / "bgw-dump-20260102-030405.json"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert default_dump_path(when) == tmp_path / "home" / ".local" / "state" / "bgw" / "dumps" / (
        "bgw-dump-20260102-030405.json"
    )


def test_default_dump_path_renders_the_timestamp_in_utc():
    from datetime import timedelta, timezone

    local = datetime(2026, 9, 19, 9, 5, 9, tzinfo=timezone(timedelta(hours=2)))
    assert default_dump_path(local).name == "bgw-dump-20260919-070509.json"


def test_write_dump_file_writes_owner_only_pretty_json_with_secrets_intact_and_round_trips(tmp_path):
    path = tmp_path / "nested" / "dump.json"
    write_dump_file(path, SNAPSHOT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "nested").stat().st_mode) == 0o700
    text = path.read_text()
    assert "super-secret" in text
    assert text.endswith("\n")
    assert len(text.split("\n")) > 5
    assert read_dump_file(path) == SNAPSHOT
    assert not list(tmp_path.glob("**/*.tmp"))


def test_write_dump_file_is_byte_compatible_with_the_typescript_layout():
    expected = (
        "{\n"
        '  "meta": {\n'
        '    "schema": 2,\n'
        '    "firmware": "6.35.8",\n'
        '    "ts": "2026-09-20T00:00:00.000Z",\n'
        '    "routerHost": "192.168.1.254"\n'
        "  },\n"
        '  "services": [\n'
        "    {\n"
        '      "name": "Mosh",\n'
        '      "extMinPort": 60001,\n'
        '      "extMaxPort": 60010,\n'
        '      "intStartPort": 60001,\n'
        '      "protocol": "UDP"\n'
        "    }\n"
        "  ],\n"
        '  "forwards": [\n'
        "    {\n"
        '      "service": "Mosh",\n'
        '      "deviceLabel": "host-a",\n'
        '      "deviceMac": "02:0a:0b:0c:0d:01"\n'
        "    }\n"
        "  ],\n"
        '  "reservations": [\n'
        "    {\n"
        '      "mac": "02:0a:0b:0c:0d:01",\n'
        '      "ip": "192.168.1.65"\n'
        "    }\n"
        "  ],\n"
        '  "forms": {\n'
        '    "dosprotect": {\n'
        '      "reflexive": "on"\n'
        "    }\n"
        "  },\n"
        '  "tables": {\n'
        '    "ipalloc": [\n'
        "      {\n"
        '        "MAC Address": "02:0a:0b:0c:0d:01"\n'
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
    )
    assert dump_json_text(GOOD) == expected
    empty = dump_json_text(SNAPSHOT)
    assert '  "services": [],\n' in empty
    assert '  "tables": {}\n' in empty


def test_dump_json_keeps_non_ascii_unescaped_like_json_stringify():
    text = dump_json_text(Snapshot(meta=SNAPSHOT.meta, forms={"wconfig": {"ssidname11": "Café"}}))
    assert '"Café"' in text


def test_snapshot_dict_round_trip():
    assert snapshot_from_dict(snapshot_to_dict(GOOD)) == GOOD


def test_write_dump_file_replaces_an_existing_file_atomically(tmp_path):
    path = tmp_path / "dump.json"
    path.write_text("old")
    write_dump_file(path, SNAPSHOT)
    assert read_dump_file(path) == SNAPSHOT
    assert [p.name for p in tmp_path.iterdir()] == ["dump.json"]


def test_write_dump_file_cleans_up_its_temp_file_when_writing_fails(tmp_path, monkeypatch):
    path = tmp_path / "dump.json"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        write_dump_file(path, SNAPSHOT)
    assert list(tmp_path.iterdir()) == []


def test_read_dump_file_rejects_missing_malformed_and_wrong_schema_files(tmp_path):
    with pytest.raises(DumpFileError, match="Cannot read dump file"):
        read_dump_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(DumpFileError, match="not valid JSON"):
        read_dump_file(bad)
    wrong = tmp_path / "wrong.json"
    d = snapshot_to_dict(SNAPSHOT)
    d["meta"]["schema"] = 3
    wrong.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match="schema"):
        read_dump_file(wrong)


def test_read_dump_file_rejects_a_non_utf8_file_as_a_dump_file_error(tmp_path):
    # TS decodes with replacement characters and then fails JSON.parse -> DumpFileError (exit 2);
    # Python must not leak UnicodeDecodeError (exit 1).
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00{not json")
    with pytest.raises(DumpFileError, match="Cannot read dump file .*binary.json.*not valid UTF-8"):
        read_dump_file(binary)


def test_write_dump_file_does_not_chmod_pre_existing_directories(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    write_dump_file(loose / "dump.json", SNAPSHOT)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o755


def test_write_dump_file_locks_down_every_directory_it_creates(tmp_path):
    write_dump_file(tmp_path / "a" / "b" / "dump.json", SNAPSHOT)
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "a" / "b").stat().st_mode) == 0o700


def test_read_dump_file_rejects_a_schema_1_dump_and_tells_the_owner_to_re_dump(tmp_path):
    # Schema 1 captured Advanced Wi-Fi under `forms.wconfig_unified`; schema 2 renamed the page to
    # `wconfig`. The diff only walks current form pages and skips ones the dump lacks, so a schema-1
    # dump would report `identical` while its Wi-Fi settings were never compared at all.
    legacy = tmp_path / "v1.json"
    d = snapshot_to_dict(SNAPSHOT)
    del d["reservations"]
    d["meta"]["schema"] = 1
    legacy.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match=re.compile(r"schema 1.*re-dump|re-dump.*schema 1", re.I)):
        read_dump_file(legacy)


def test_read_dump_file_rejects_schema_3(tmp_path):
    future = tmp_path / "v3.json"
    d = snapshot_to_dict(SNAPSHOT)
    d["meta"]["schema"] = 3
    future.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match="schema"):
        read_dump_file(future)


def test_read_dump_file_rejects_a_hand_edited_reservation_that_is_not_a_mac_ipv4_pair(tmp_path):
    release = tmp_path / "release.json"
    d = snapshot_to_dict(SNAPSHOT)
    d["reservations"] = [{"mac": "02:0a:0b:0c:0d:02", "ip": "normal"}]
    release.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match="invalid reservation entry"):
        read_dump_file(release)
    bad_mac = tmp_path / "badmac.json"
    d["reservations"] = [{"mac": "nope", "ip": "1.2.3.4"}]
    bad_mac.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match=re.escape('invalid reservation entry ({"mac":"nope","ip":"1.2.3.4"})')):
        read_dump_file(bad_mac)
    not_object = tmp_path / "notobj.json"
    d["reservations"] = ["02:0a:0b:0c:0d:02"]
    not_object.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match="invalid reservation entry"):
        read_dump_file(not_object)


# A dump is the "truth" restore writes onto the router and --prune deletes against. Structural
# gaps must not load: a file that merely has the right top-level keys could otherwise produce an
# executable removal plan from garbage.
def rejects(tmp_path: Path, mutate: Callable[[dict[str, Any]], None], pattern: str) -> None:
    path = tmp_path / "d.json"
    d = snapshot_to_dict(GOOD)
    mutate(d)
    path.write_text(json.dumps(d))
    with pytest.raises(DumpFileError, match=pattern):
        read_dump_file(path)


def test_read_dump_file_accepts_a_fully_formed_schema_2_dump(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(snapshot_to_dict(GOOD)))
    assert read_dump_file(path) == GOOD


def test_read_dump_file_rejects_a_non_object_document(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]")
    with pytest.raises(DumpFileError, match="not an object"):
        read_dump_file(path)


def test_read_dump_file_rejects_schema_2_dumps_missing_meta_fields(tmp_path):
    rejects(tmp_path, lambda d: d["meta"].pop("firmware"), r"meta\.firmware")
    rejects(tmp_path, lambda d: d["meta"].pop("ts"), r"meta\.ts")
    rejects(tmp_path, lambda d: d["meta"].__setitem__("routerHost", 5), r"meta\.routerHost")
    rejects(tmp_path, lambda d: d.__setitem__("meta", "x"), r"meta\.schema must be 2")
    rejects(tmp_path, lambda d: d["meta"].__setitem__("schema", True), r"meta\.schema must be 2")


def test_read_dump_file_rejects_schema_2_dumps_without_a_reservations_array(tmp_path):
    rejects(tmp_path, lambda d: d.pop("reservations"), "reservations")
    rejects(tmp_path, lambda d: d.__setitem__("services", {}), "services must be an array")
    rejects(tmp_path, lambda d: d.__setitem__("forwards", None), "forwards must be an array")


def test_read_dump_file_rejects_malformed_service_and_forward_entries(tmp_path):
    rejects(tmp_path, lambda d: d["services"].__setitem__(0, {"name": "x"}), "service entry")
    rejects(tmp_path, lambda d: d["services"][0].__setitem__("extMinPort", "60001"), "service entry")
    rejects(tmp_path, lambda d: d["services"][0].__setitem__("extMinPort", 1.5), "service entry")
    rejects(tmp_path, lambda d: d["services"][0].__setitem__("extMinPort", True), "service entry")
    rejects(tmp_path, lambda d: d["forwards"].__setitem__(0, {"service": "x", "deviceLabel": "y"}), "forward entry")
    rejects(tmp_path, lambda d: d["forwards"][0].__setitem__("deviceMac", "not-a-mac"), "forward entry")


def test_read_dump_file_accepts_integral_float_ports_like_number_is_integer(tmp_path):
    path = tmp_path / "float.json"
    d = snapshot_to_dict(GOOD)
    d["services"][0]["extMinPort"] = 60001.0
    path.write_text(json.dumps(d))
    assert read_dump_file(path).services[0].ext_min_port == 60001


def test_read_dump_file_rejects_malformed_form_and_table_sections(tmp_path):
    rejects(tmp_path, lambda d: d["forms"].__setitem__("dosprotect", "on"), r"forms\.dosprotect")
    rejects(tmp_path, lambda d: d["forms"].__setitem__("dosprotect", {"reflexive": 1}), r"forms\.dosprotect")
    rejects(tmp_path, lambda d: d.__setitem__("forms", []), "forms must be an object")
    rejects(tmp_path, lambda d: d["tables"].__setitem__("ipalloc", {"a": "b"}), r"tables\.ipalloc")
    rejects(tmp_path, lambda d: d["tables"].__setitem__("ipalloc", [{"a": 1}]), r"tables\.ipalloc")
    rejects(tmp_path, lambda d: d.__setitem__("tables", 3), "tables must be an object")
