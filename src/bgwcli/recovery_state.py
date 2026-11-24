"""Private recovery intent, never a queue of POSTs. CLI callers hold the router session lock."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import filesystem
from .client import normalize_origin
from .errors import BgwError
from .snapshot import Snapshot, forward_key, reservation_key, service_key
from .snapshot_diff import RESTORABLE_PAGES


class RecoveryIntentConflictError(BgwError, OSError):
    """The router's intent file records another unfinished recovery (different dump or pages).

    An OSError so checkpoint callers report it like every other checkpoint fault (exit 2)."""


class Checkpoint(Protocol):
    def is_active(self) -> bool: ...
    def begin(self, scope: Mapping[str, Any] | None = None) -> None: ...
    def finish(self) -> None: ...


class ScopedCheckpoint(Protocol):
    """Optional checkpoint extension: what one recovery covers, when it began and how many of its
    runs in a row sent nothing (see autorestore's RECOVERY_MAX_AGE_SECONDS / RECOVERY_IDLE_RUN_LIMIT)."""

    def begin(self, scope: Mapping[str, Any] | None = None) -> None: ...
    def recorded_scope(self) -> dict[str, Any] | None: ...
    def started_at(self) -> float | None: ...
    def ensure_started(self) -> None: ...
    def note_run(self, *, sent: bool) -> int: ...


class FailureTracker(Protocol):
    """Optional checkpoint extension: consecutive identical failed runs of one recovery intent."""

    def failure_count(self) -> int: ...
    def record_failure(self, fingerprint: str) -> int: ...


# `_publish` names its temporaries with this prefix (tempfile.mkstemp: eight random characters).
_TEMPORARY = re.compile(r"\.recovery-[a-z0-9_]{8}")
# A live writer holds its temporary for milliseconds; anything this old is a crashed run's leftover.
_STALE_TEMPORARY_SECONDS = 3600

_IDENTITY_KEYS = ("version", "origin", "fingerprint", "pages")
# Fields kept beside the identity across resumes of the same intent (never part of its identity).
_CARRIED_KEYS = ("failures", "scope", "startedAt", "idleRuns", "pendingLanMove")
_now = time.time  # seam for tests: the intent's age is wall-clock time, it must survive reboots
_SCOPE_SECTIONS = ("services", "forwards", "reservations")


def _valid_scope(value: object) -> dict[str, Any] | None:
    """A recorded scope in its exact shape, else None (an older record: the whole difference)."""
    if not isinstance(value, dict):
        return None
    sections = {name: value.get(name) for name in _SCOPE_SECTIONS}
    forms = value.get("forms")
    if not all(isinstance(v, list) and all(isinstance(i, str) for i in v) for v in sections.values()):
        return None
    if not isinstance(forms, dict) or not all(
        isinstance(page, str) and isinstance(fields, list) and all(isinstance(f, str) for f in fields)
        for page, fields in forms.items()
    ):
        return None
    return {**sections, "forms": forms}


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


class RecoveryCheckpoint:
    """Bind one router's unfinished recovery to the desired values and selected pages.

    Constructors and reads never create files. Writers must serialize through the router session
    coordinator (as the CLI does); library callers supplying a store own that serialization.
    """

    def __init__(
        self, host: str, dump: Snapshot, pages: Sequence[str] | None = None, *, root: Path | None = None,
    ) -> None:
        origin = normalize_origin(host)
        selected = sorted(set(RESTORABLE_PAGES if pages is None else pages))
        desired = {
            "services": sorted(service_key(s) for s in dump.services) if "services" in selected else [],
            "forwards": sorted(forward_key(f) for f in dump.forwards) if "apphosting" in selected else [],
            "reservations": sorted((reservation_key(r), r.ip) for r in dump.reservations)
            if "ipalloc" in selected else [],
            "forms": {p: dump.forms[p] for p in selected if p in dump.forms},
        }
        fingerprint = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
        self._record = {"version": 1, "origin": origin, "fingerprint": fingerprint, "pages": selected}
        if root is None:
            state_base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
            root = state_base / "bgw" / "recovery"
        else:
            state_base = root.parent
        self._state_base = state_base.resolve()
        # Keep application components visible: resolving them here would hide symlinks
        # before publication can refuse to alter their targets. State-base aliases are valid.
        application_root = self._state_base / root.relative_to(state_base)
        self.path = application_root / f"{hashlib.sha256(origin.encode()).hexdigest()[:24]}.recovery.json"

    def is_active(self) -> bool:
        """True when this intent is recorded. No file (or a corrupt one) is "no intent"; a record
        that exists but cannot be read raises OSError: the answer is unknown, not "no"."""
        return self._identity(self._read_record()) == self._record

    @staticmethod
    def _identity(record: object) -> dict[str, object] | None:
        """The intent's identity fields; the failure counter beside them is not part of it."""
        if not isinstance(record, dict):
            return None
        return {key: record[key] for key in _IDENTITY_KEYS if key in record}

    def failure_count(self) -> int:
        """Consecutive runs of this intent that ended with the same failure (0: none recorded)."""
        record = self._read_record()
        if self._identity(record) != self._record:
            return 0
        failures = record.get("failures") if isinstance(record, dict) else None
        count = failures.get("count") if isinstance(failures, dict) else None
        return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0

    def record_failure(self, fingerprint: str) -> int:
        """Count one more failed run of this intent: the same failure fingerprint as the last run
        increments the count, a different one restarts it at 1. Only a hash of the failure is kept.
        Returns the new count, or 0 when this intent is not recorded (nothing to count against)."""
        record = self._read_record()
        if self._identity(record) != self._record:
            return 0
        previous = record.get("failures") if isinstance(record, dict) else None
        count = 1
        if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
            count = self.failure_count() + 1
        carried = {key: record[key] for key in _CARRIED_KEYS if isinstance(record, dict) and key in record}
        self._publish({**self._record, **carried, "failures": {"fingerprint": fingerprint, "count": count}})
        return count

    def _read_record(self) -> object:
        """The record, read from one no-follow descriptor that must be our own regular file: a
        symlink, a non-file or another user's file is a fault (OSError), never an intent or counter.
        A missing or malformed file is "no record"."""
        try:
            # O_NONBLOCK: opening a FIFO nobody writes to must fail the type check below, never wait
            # for a writer while the caller holds the session lock.
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"Recovery record {self.path} is not a regular file; remove it after verifying.")
            if info.st_uid != os.geteuid():
                raise PermissionError(
                    f"Recovery record {self.path} is owned by another user (uid {info.st_uid}); "
                    "remove it or fix its ownership after verifying the gateway."
                )
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                content = stream.read()
        finally:
            os.close(descriptor)
        try:
            return json.loads(content.decode("utf-8"))
        except ValueError:
            return None

    def begin(self, scope: Mapping[str, Any] | None = None) -> None:
        """Record this intent. A new intent stores `scope` (what the recovery covers: the entries
        missing and the form fields differing when the reset was detected; None = everything that
        differs) and its start time. Resuming the same intent keeps its own scope, start time, idle-run
        count and failure history: a later run never widens or restarts a recovery."""
        filesystem.reject_filesystem_root(self.path.parent, description="Recovery checkpoint directory")
        filesystem.ensure_directory(self._state_base, description="Recovery directory")
        filesystem.ensure_directory(
            self.path.parent, boundary=self._state_base, private=True, description="Recovery directory",
        )
        self._reap_stale_temporaries()
        # Checked once the directories are prepared (and their owner access repaired).
        existing = self._read_record()
        if isinstance(existing, dict) and "fingerprint" in existing and self._identity(existing) != self._record:
            raise RecoveryIntentConflictError(
                f"{self.path} records another unfinished recovery (a different dump or page selection); "
                "finish it with the matching dump, or remove the file after verifying the gateway."
            )
        record: dict[str, object] = dict(self._record)
        if self._identity(existing) == self._record and isinstance(existing, dict):
            # Resuming the same intent keeps its failure history (the cross-run limit counts runs), its
            # scope and its start time (the age limit counts from the reset, not from the last run).
            for key in _CARRIED_KEYS:
                if key in existing:
                    record[key] = existing[key]
            record.setdefault("startedAt", _now())
        else:
            if scope is not None:
                record["scope"] = {
                    **{name: sorted(set(scope.get(name, ()))) for name in _SCOPE_SECTIONS},
                    "forms": {page: sorted(set(fields)) for page, fields in sorted(scope.get("forms", {}).items())},
                }
            record["startedAt"] = _now()
            record["idleRuns"] = 0
        self._publish(record)

    def _active_record(self) -> dict[str, Any] | None:
        record = self._read_record()
        return record if isinstance(record, dict) and self._identity(record) == self._record else None

    def recorded_scope(self) -> dict[str, Any] | None:
        """What this recovery covers, or None (no intent, or an older record without a scope: the
        whole difference)."""
        record = self._active_record()
        return _valid_scope(record.get("scope")) if record is not None else None

    def started_at(self) -> float | None:
        """When this recovery began (epoch seconds), or None when not recorded (an older record)."""
        record = self._active_record()
        return _positive_number(record.get("startedAt")) if record is not None else None

    def ensure_started(self) -> None:
        """Give an older record without a start time one now, so it expires within the age limit."""
        record = self._active_record()
        if record is not None and _positive_number(record.get("startedAt")) is None:
            self._publish({**record, "startedAt": _now()})

    def idle_runs(self) -> int:
        record = self._active_record()
        count = record.get("idleRuns") if record is not None else None
        return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0

    def note_run(self, *, sent: bool) -> int:
        """Count consecutive runs of this recovery that sent nothing: a run that sent resets the count
        to 0, one that sent nothing adds 1. Returns the new count (0 when this intent is not recorded)."""
        record = self._active_record()
        if record is None:
            return 0
        count = 0 if sent else self.idle_runs() + 1
        if record.get("idleRuns") != count:
            self._publish({**record, "idleRuns": count})
        return count

    def pending_lan_move(self) -> dict[str, Any] | None:
        """The LAN move (dhcpserver ipaddr/ipmask/dhcp) a run of this recovery posted and the gateway
        answered: {"page", "fields"}, or None (none pending, or an older record without one)."""
        record = self._active_record()
        move = record.get("pendingLanMove") if record is not None else None
        if not isinstance(move, dict) or not isinstance(move.get("page"), str):
            return None
        fields = move.get("fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
            return None
        return {"page": move["page"], "fields": sorted(set(fields))}

    def set_pending_lan_move(self, page: str, fields: Sequence[str]) -> None:
        """Remember an answered LAN move until a later run reads whether it applied."""
        record = self._active_record()
        if record is not None:
            self._publish({**record, "pendingLanMove": {"page": page, "fields": sorted(set(fields))}})

    def clear_pending_lan_move(self) -> None:
        record = self._active_record()
        if record is not None and "pendingLanMove" in record:
            self._publish({key: value for key, value in record.items() if key != "pendingLanMove"})

    def _reap_stale_temporaries(self) -> None:
        """Remove crashed runs' `.recovery-*` temporaries: our own plain files, an hour or older.
        Housekeeping only: it never fails the run and never touches any other name."""
        try:
            entries = list(os.scandir(self.path.parent))
        except OSError:
            return
        now = time.time()
        for entry in entries:
            if not _TEMPORARY.fullmatch(entry.name):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                ours = stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                if ours and now - info.st_mtime > _STALE_TEMPORARY_SECONDS:
                    os.unlink(entry.path)
            except OSError:
                continue

    def _publish(self, record: dict[str, object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".recovery-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            filesystem.sync_directory(self.path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def finish(self) -> None:
        if self.is_active():
            filesystem.reject_filesystem_root(self.path.parent, description="Recovery checkpoint directory")
            directory = self._state_base
            for component in self.path.parent.relative_to(self._state_base).parts:
                directory = directory / component
                filesystem.reject_directory_symlink(directory, description="Recovery directory")
            self.path.unlink(missing_ok=True)
            filesystem.sync_directory(self.path.parent)
