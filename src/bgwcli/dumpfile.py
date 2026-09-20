"""Dump files: schema-2 JSON written exactly as the TypeScript CLI writes it (camelCase keys,
2-space indent, trailing newline, mode 0600 in 0700 directories, atomic temp+rename), and the
matching loader with full structural validation. Ported 1:1 from BGW320-CLI src/dumpfile.ts.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DumpFileError
from .snapshot import (
    IPV4_PATTERN,
    MAC_PATTERN,
    Snapshot,
    SnapshotForward,
    SnapshotMeta,
    SnapshotReservation,
    SnapshotService,
)


def default_dump_path(now: datetime | None = None) -> Path:
    """$BGW_DUMP_DIR, else $XDG_STATE_HOME/bgw/dumps, else ~/.local/state/bgw/dumps; timezone.utc timestamped name."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    base = os.environ.get("BGW_DUMP_DIR") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "bgw" / "dumps"
    )
    return Path(base) / f"bgw-dump-{now.strftime('%Y%m%d-%H%M%S')}.json"


# --- JSON shape -----------------------------------------------------------------------------------
# Key names and order are the TS object literal order; both CLIs must produce byte-identical files.


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "meta": {
            "schema": snapshot.meta.schema,
            "firmware": snapshot.meta.firmware,
            "ts": snapshot.meta.ts,
            "routerHost": snapshot.meta.router_host,
        },
        "services": [
            {
                "name": s.name,
                "extMinPort": s.ext_min_port,
                "extMaxPort": s.ext_max_port,
                "intStartPort": s.int_start_port,
                "protocol": s.protocol,
            }
            for s in snapshot.services
        ],
        "forwards": [
            {"service": f.service, "deviceLabel": f.device_label, "deviceMac": f.device_mac} for f in snapshot.forwards
        ],
        "reservations": [{"mac": r.mac, "ip": r.ip} for r in snapshot.reservations],
        "forms": {page: dict(form) for page, form in snapshot.forms.items()},
        "tables": {page: [dict(row) for row in rows] for page, rows in snapshot.tables.items()},
    }


def snapshot_from_dict(value: Mapping[str, Any]) -> Snapshot:
    """Build a Snapshot from an already-validated schema-2 dict (see _snapshot_problem)."""
    meta = value["meta"]
    return Snapshot(
        meta=SnapshotMeta(schema=2, firmware=meta["firmware"], ts=meta["ts"], router_host=meta["routerHost"]),
        services=[
            SnapshotService(
                name=s["name"],
                ext_min_port=int(s["extMinPort"]),
                ext_max_port=int(s["extMaxPort"]),
                int_start_port=int(s["intStartPort"]),
                protocol=s["protocol"],
            )
            for s in value["services"]
        ],
        forwards=[
            SnapshotForward(service=f["service"], device_label=f["deviceLabel"], device_mac=f["deviceMac"])
            for f in value["forwards"]
        ],
        reservations=[SnapshotReservation(mac=r["mac"], ip=r["ip"]) for r in value["reservations"]],
        forms={page: dict(form) for page, form in value["forms"].items()},
        tables={page: [dict(row) for row in rows] for page, rows in value["tables"].items()},
    )


def dump_json_text(snapshot: Snapshot) -> str:
    return json.dumps(snapshot_to_dict(snapshot), indent=2, ensure_ascii=False) + "\n"


# --- Writing --------------------------------------------------------------------------------------


def _missing_path_components(target: Path) -> list[Path]:
    components: list[Path] = []
    current = target
    while current != current.parent:
        if current.exists():
            break
        components.insert(0, current)
        current = current.parent
    return components


def write_dump_file(path: str | os.PathLike[str], snapshot: Snapshot) -> None:
    target = Path(path)
    directory = target.parent
    # Only directories this call creates are locked down to 0700; pre-existing ones keep their mode.
    missing = _missing_path_components(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for component in missing:
        os.chmod(component, 0o700)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # now owned by the file object
            handle.write(dump_json_text(snapshot))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except BaseException:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- Reading + validation -------------------------------------------------------------------------


def read_dump_file(path: str | os.PathLike[str]) -> Snapshot:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise DumpFileError(f"Cannot read dump file '{path}': {exc}") from exc
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError is a ValueError, not an OSError; without this it would escape as exit 1.
        raise DumpFileError(f"Cannot read dump file '{path}': not valid UTF-8 text.") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise DumpFileError(f"Dump file '{path}' is not valid JSON.") from exc
    # Schema 1 (pre-2026-09-19) stored Advanced Wi-Fi under `forms.wconfig_unified`; the current
    # page set compares `forms.wconfig` and skips pages the dump lacks, so a schema-1 file would
    # diff as `identical` with its Wi-Fi settings never compared. Refuse it rather than understate drift.
    if _is_legacy_schema(value):
        raise DumpFileError(
            f"Dump file '{path}' is schema 1 (pre-reservation, pre-wconfig); "
            "re-dump with 'bgw dump' before diffing or restoring."
        )
    problem = _snapshot_problem(value)
    if problem is not None:
        raise DumpFileError(f"Dump file '{path}' is not a valid schema-2 dump: {problem}")
    # A dump file is a plain JSON file the owner can hand-edit. A reservation's `ip` is copied
    # straight into the follow-up POST's option value, and the router always offers `normal`
    # ("Address from DHCP pool") on the entry form — so an `ip` that is not a literal IPv4 address
    # would quietly turn a restore into a release. Reject those on load rather than post them.
    for entry in value["reservations"]:
        _require_valid_reservation(entry, path)
    return snapshot_from_dict(value)


def _require_valid_reservation(entry: Any, path: str | os.PathLike[str]) -> None:
    mac = entry.get("mac") if isinstance(entry, dict) else None
    ip = entry.get("ip") if isinstance(entry, dict) else None
    if not isinstance(mac, str) or not isinstance(ip, str) or not MAC_PATTERN.match(mac) or not IPV4_PATTERN.match(ip):
        raise DumpFileError(f"Dump file '{path}' has an invalid reservation entry ({_json_compact(entry)})")


def _json_compact(value: Any) -> str:
    # Mirrors JSON.stringify(value) for error messages.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _is_legacy_schema(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    meta = value.get("meta")
    return isinstance(meta, dict) and meta.get("schema") == 1 and not isinstance(meta.get("schema"), bool)


def _is_record(v: Any) -> bool:
    return isinstance(v, dict)


def _is_string_map(v: Any) -> bool:
    return isinstance(v, dict) and all(isinstance(x, str) for x in v.values())


def _is_integer(v: Any) -> bool:
    # Number.isInteger: JSON 60001 and 60001.0 are the same number in JS; bool is not a number here.
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, float) and v.is_integer())


# Structural validation of a schema-2 dump. The dump is the truth `restore` writes onto the
# router and `--prune` deletes against, so a file with the right top-level keys but malformed
# contents must not load: it could otherwise yield an executable plan built from garbage.
# Returns a description of the first problem, or None when the value is a Snapshot.
def _snapshot_problem(value: Any) -> str | None:
    if not _is_record(value):
        return "not an object"
    meta = value.get("meta")
    if not _is_record(meta) or meta.get("schema") != 2 or isinstance(meta.get("schema"), bool):
        return "meta.schema must be 2"
    for name in ("firmware", "ts", "routerHost"):
        if not isinstance(meta.get(name), str):
            return f"meta.{name} must be a string"
    services = value.get("services")
    if not isinstance(services, list):
        return "services must be an array"
    for entry in services:
        if not _is_service(entry):
            return f"invalid service entry ({_json_compact(entry)})"
    forwards = value.get("forwards")
    if not isinstance(forwards, list):
        return "forwards must be an array"
    for entry in forwards:
        if not _is_forward(entry):
            return f"invalid forward entry ({_json_compact(entry)})"
    if not isinstance(value.get("reservations"), list):
        return "reservations must be an array"
    forms = value.get("forms")
    if not _is_record(forms):
        return "forms must be an object"
    for page, form in forms.items():
        if not _is_string_map(form):
            return f"forms.{page} must be a map of string values"
    tables = value.get("tables")
    if not _is_record(tables):
        return "tables must be an object"
    for page, rows in tables.items():
        if not isinstance(rows, list) or not all(_is_string_map(row) for row in rows):
            return f"tables.{page} must be an array of string-valued rows"
    return None


def _is_service(entry: Any) -> bool:
    return (
        _is_record(entry)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("protocol"), str)
        and all(_is_integer(entry.get(k)) for k in ("extMinPort", "extMaxPort", "intStartPort"))
    )


def _is_forward(entry: Any) -> bool:
    return (
        _is_record(entry)
        and isinstance(entry.get("service"), str)
        and isinstance(entry.get("deviceLabel"), str)
        and isinstance(entry.get("deviceMac"), str)
        and MAC_PATTERN.match(entry["deviceMac"]) is not None
    )
