"""Dump files: schema-2 JSON written exactly as the TypeScript CLI writes it (camelCase keys,
2-space indent, trailing newline, mode 0600 in 0700 directories, atomic temp+rename), and the
matching loader with full structural validation. Ported 1:1 from BGW320-CLI src/dumpfile.ts.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
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
# Key names and order are the TS object literal order, so both CLIs produce byte-identical files
# for the same router, except that this CLI adds `formSecrets` when a form page has password-type
# controls (the TS CLI ignores the key on read and never writes it).


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    value = {
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
    # Only dumps of pages with password-type controls carry this key; every other dump keeps the
    # original schema-2 shape. Older readers ignore it (the loader never rejected unknown keys).
    secrets = {page: list(names) for page, names in snapshot.form_secrets.items() if names}
    if secrets:
        value["formSecrets"] = secrets
    # Only pages with a text/select field whose real value is the string "<unchecked>" carry a
    # record naming those fields; every other "<unchecked>" in a dump is an off checkbox/radio.
    # Absent in every other dump, and a dump without it is read as it always was.
    literal = {page: list(names) for page, names in snapshot.form_unchecked_text.items() if names}
    if literal:
        value["formUncheckedText"] = literal
    return value


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
        form_secrets={page: list(names) for page, names in value.get("formSecrets", {}).items()},
        form_unchecked_text={page: list(names) for page, names in value.get("formUncheckedText", {}).items()},
    )


def snapshot_problem(snapshot: Snapshot) -> str | None:
    """Why the loader would refuse this snapshot once written (None when it would read it back)."""
    return _snapshot_problem(snapshot_to_dict(snapshot))


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


def preflight_dump_target(path: str | os.PathLike[str]) -> None:
    """Refuse a dump target the write could never complete, before anything is read or created.

    Checked with lstat (the final component is never followed): an existing directory, a symlink, a
    file owned by another user and any other non-regular entry are refused, as is a path whose nearest
    existing ancestor is not a directory. A missing target under a creatable directory chain passes.
    Nothing is created. The messages name the path as given."""
    shown = str(path)
    target = Path(path)
    through_file = NotADirectoryError(
        f"Output path runs through a file, not a directory; nothing was written: {shown}"
    )
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        missing = _missing_path_components(target.parent)
        nearest = missing[0].parent if missing else target.parent
        try:
            ancestor_is_dir = stat.S_ISDIR(os.stat(nearest).st_mode)
        except OSError:
            return  # an unreadable ancestor: the write itself reports it
        if not ancestor_is_dir:
            raise through_file from None
        _require_writable_directory(nearest, shown)
        return
    except NotADirectoryError:
        raise through_file from None
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        raise IsADirectoryError(f"Output path is a directory, not a dump file; nothing was written: {shown}")
    if stat.S_ISLNK(mode):
        raise PermissionError(
            f"Output path is a symlink; refusing to write through it; nothing was written: {shown}"
        )
    if not stat.S_ISREG(mode):
        raise PermissionError(f"Output path is not a regular file; nothing was written: {shown}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"Output file is owned by another user; nothing was written: {shown}")
    _require_writable_directory(target.parent, shown)


def _require_writable_directory(directory: Path, shown: str) -> None:
    """The write creates and renames a file in `directory` (or, for a missing chain, the nearest
    existing ancestor), so it must be writable and searchable."""
    if not os.access(directory, os.W_OK | os.X_OK):
        raise PermissionError(f"Output directory is not writable; nothing was written: {shown}")


def _naming_target(exc: OSError, path: str | os.PathLike[str]) -> OSError:
    """The same error class, naming the user's target instead of the internal temporary file."""
    reason = exc.strerror or str(exc)
    if exc.errno is None:
        return type(exc)(f"{reason}: '{path}'")
    return type(exc)(exc.errno, reason, str(path))


def write_dump_file(path: str | os.PathLike[str], snapshot: Snapshot) -> None:
    preflight_dump_target(path)
    target = Path(path)
    directory = target.parent
    # Only directories this call itself creates are locked down to 0700 and cleaned up on failure.
    # A component that appears between the scan and its mkdir was made by someone else: it is not
    # ours, so it keeps its mode and is never removed.
    created: list[Path] = []
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    fd: int | None = None
    try:
        try:
            for component in _missing_path_components(directory):
                try:
                    component.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                created.append(component)
            for component in created:
                os.chmod(component, 0o700)
        except OSError as exc:
            raise _naming_target(exc, path) from None
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise _naming_target(exc, path) from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None  # now owned by the file object
                handle.write(dump_json_text(snapshot))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _naming_target(exc, path) from None
        try:
            os.replace(temporary, target)
        except OSError as exc:
            raise _naming_target(exc, path) from None
        os.chmod(target, 0o600)
    except BaseException:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        # Leave no empty directory chain behind: remove only what this call created, deepest first.
        # rmdir refuses a non-empty directory, so anything that appeared in the meantime is kept.
        for component in reversed(created):
            with contextlib.suppress(OSError):
                component.rmdir()
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
    seen_names: set[str] = set()
    for entry in services:
        name = entry["name"].strip().casefold()
        if name in seen_names:
            # The gateway keys services by name; restoring two of one name would add a second service.
            return f"service name '{name}' appears more than once"
        seen_names.add(name)
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
    # Optional (absent in dumps without password-type controls); when present it decides what
    # diff/restore redact, so a malformed one is refused rather than ignored.
    secrets = value.get("formSecrets", {})
    if not _is_record(secrets):
        return "formSecrets must be an object"
    for page, names in secrets.items():
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return f"formSecrets.{page} must be an array of field names"
    literal = value.get("formUncheckedText", {})
    if not _is_record(literal):
        return "formUncheckedText must be an object"
    for page, names in literal.items():
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            return f"formUncheckedText.{page} must be an array of field names"
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
