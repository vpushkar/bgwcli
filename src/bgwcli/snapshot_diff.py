"""Compare a dump Snapshot against a live Snapshot. Ported 1:1 from BGW320-CLI src/snapshot-diff.ts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .snapshot import (
    FORM_PAGES,
    Snapshot,
    SnapshotForward,
    SnapshotReservation,
    SnapshotService,
    forward_key,
    reservation_key,
    service_key,
)

T = TypeVar("T")
@dataclass(frozen=True)
class FormFieldDiff:
    field: str
    dump: str | None
    live: str | None


@dataclass(frozen=True)
class ReservationChange:
    mac: str
    dump_ip: str
    live_ip: str


@dataclass(frozen=True)
class EntryDiff(Generic[T]):
    missing: list[T] = field(default_factory=list)
    extra: list[T] = field(default_factory=list)


@dataclass(frozen=True)
class ReservationDiff:
    missing: list[SnapshotReservation] = field(default_factory=list)
    changed: list[ReservationChange] = field(default_factory=list)
    extra: list[SnapshotReservation] = field(default_factory=list)


@dataclass(frozen=True)
class SnapshotDiff:
    identical: bool
    services: EntryDiff[SnapshotService]
    forwards: EntryDiff[SnapshotForward]
    reservations: ReservationDiff
    forms: dict[str, list[FormFieldDiff]]
    firmware_changed: bool


def compared_form_pages(*, include_lan: bool) -> list[str]:
    return [page for page in FORM_PAGES if include_lan or page != "etherlan"]


def diff_snapshots(dump: Snapshot, live: Snapshot, *, include_lan: bool) -> SnapshotDiff:
    services = _set_difference(dump.services, live.services, service_key)
    forwards = _set_difference(dump.forwards, live.forwards, forward_key)
    reservations = _diff_reservations(dump.reservations, live.reservations)
    forms: dict[str, list[FormFieldDiff]] = {}
    for page in compared_form_pages(include_lan=include_lan):
        dump_form = dump.forms.get(page)
        live_form = live.forms.get(page)
        # A dump cannot claim a page it never captured. Schema-1 dumps predate `forms.wconfig`, so
        # comparing one against a live router that has it would otherwise report every wconfig
        # field as `dump: None` and keep `identical` (and restore_converged) false forever. The
        # reverse — the dump has the page and the router no longer serves it — is real drift and
        # is still reported field by field with `live: None`.
        if dump_form is None:
            continue
        names = set(dump_form) | set(live_form or {})
        changed: list[FormFieldDiff] = []
        for name in sorted(names):
            dump_value = dump_form.get(name)
            live_value = live_form.get(name) if live_form is not None else None
            if dump_value != live_value:
                changed.append(FormFieldDiff(field=name, dump=dump_value, live=live_value))
        if changed:
            forms[page] = changed
    identical = (
        not services.missing
        and not services.extra
        and not forwards.missing
        and not forwards.extra
        and not reservations.missing
        and not reservations.changed
        and not reservations.extra
        and not forms
    )
    return SnapshotDiff(
        identical=identical,
        services=services,
        forwards=forwards,
        reservations=reservations,
        forms=forms,
        firmware_changed=dump.meta.firmware != live.meta.firmware,
    )


def _diff_reservations(dump: Sequence[SnapshotReservation], live: Sequence[SnapshotReservation]) -> ReservationDiff:
    live_by_mac = {reservation_key(r): r for r in live}
    dump_by_mac = {reservation_key(r): r for r in dump}
    missing: list[SnapshotReservation] = []
    changed: list[ReservationChange] = []
    for r in dump:
        counterpart = live_by_mac.get(reservation_key(r))
        if counterpart is None:
            missing.append(r)
        elif counterpart.ip != r.ip:
            changed.append(ReservationChange(mac=reservation_key(r), dump_ip=r.ip, live_ip=counterpart.ip))
    extra = [r for r in live if reservation_key(r) not in dump_by_mac]
    return ReservationDiff(missing=missing, changed=changed, extra=extra)


def _set_difference(dump: Sequence[T], live: Sequence[T], key_of: Callable[[T], str]) -> EntryDiff[T]:
    live_keys = {key_of(item) for item in live}
    dump_keys = {key_of(item) for item in dump}
    return EntryDiff(
        missing=[item for item in dump if key_of(item) not in live_keys],
        extra=[item for item in live if key_of(item) not in dump_keys],
    )
