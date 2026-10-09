"""Compare a dump Snapshot against a live Snapshot. Ported 1:1 from BGW320-CLI src/snapshot-diff.ts."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .errors import UsageError
from .snapshot import (
    FORM_PAGES,
    UNCHECKED,
    Snapshot,
    SnapshotForward,
    SnapshotReservation,
    SnapshotService,
    canonical_form_value,
    forward_key,
    legacy_form_spelling,
    reservation_key,
    service_key,
    split_include,
)

T = TypeVar("T")

# Page ids `diff`/`restore --include` accept: the three table sections plus every form page.
RESTORABLE_PAGES: tuple[str, ...] = ("services", "apphosting", "ipalloc", *FORM_PAGES)
@dataclass(frozen=True)
class FormFieldDiff:
    field: str
    dump: str | None
    live: str | None
    # The dump or the live page rendered this field as a type="password" control: display paths
    # redact it even though its name does not look secret. Never part of the --json shape.
    sensitive: bool = dataclasses.field(default=False, compare=False, metadata={"serialize": False})
    # The live page renders this control only disabled: a browser never posts it and its saved
    # value can never be read back, so it cannot hold restore convergence open.
    live_disabled: bool = dataclasses.field(default=False, compare=False, metadata={"serialize": False})
    # The live page does not render this control at all (for example, newer firmware dropped it): it
    # is reported, but no POST can set it and no re-read can show it, so it never holds convergence open.
    live_unrendered: bool = dataclasses.field(default=False, compare=False, metadata={"serialize": False})


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


def resolve_include(value: str | Sequence[str] | None) -> tuple[str, ...] | None:
    """`diff`/`restore --include <csv|all>` -> restorable page ids in RESTORABLE_PAGES order.

    None when the option was not given (or is `all`), meaning everything present in the dump. A
    given selection naming no page is a usage error: it must never widen to every page.
    """
    if value is None:
        return None
    names = split_include(value)
    if not names:
        raise UsageError("--include needs at least one page id (or all).")
    if len(names) == 1 and names[0].lower() == "all":
        return None
    unknown = [name for name in names if name not in RESTORABLE_PAGES]
    if unknown:
        raise UsageError(
            f"--include: unknown page(s) {', '.join(unknown)}; valid pages are {', '.join(RESTORABLE_PAGES)} (or all)"
        )
    return tuple(page for page in RESTORABLE_PAGES if page in names)


def pages_missing_from_dump(dump: Snapshot, pages: Sequence[str] | None) -> list[str]:
    """Requested form pages the dump never captured (the sections are never "missing")."""
    if pages is None:
        return []
    return [page for page in FORM_PAGES if page in pages and page not in dump.forms]


def _selected(pages: Sequence[str] | None, page: str) -> bool:
    return pages is None or page in pages


def diff_snapshots(dump: Snapshot, live: Snapshot, *, pages: Sequence[str] | None = None) -> SnapshotDiff:
    """Compare captured dump sections; `pages` narrows the selection.

    Legacy empty checkable values need `live.live_form_evidence` from extract_snapshot.
    Preserve it when copying/projecting that capture (dataclasses.replace does so);
    snapshots loaded from JSON lack HTML evidence and compare literal values instead.
    """
    services = (
        _set_difference(dump.services, live.services, service_key) if _selected(pages, "services") else EntryDiff()
    )
    forwards = (
        _set_difference(dump.forwards, live.forwards, forward_key) if _selected(pages, "apphosting") else EntryDiff()
    )
    reservations = (
        _diff_reservations(dump.reservations, live.reservations) if _selected(pages, "ipalloc") else ReservationDiff()
    )
    forms: dict[str, list[FormFieldDiff]] = {}
    for page in FORM_PAGES:
        if not _selected(pages, page):
            continue
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
        secrets = set(dump.form_secrets.get(page, ())) | set(live.form_secrets.get(page, ()))
        evidence = live.live_form_evidence.get(page)
        disabled = evidence.disabled_names if evidence is not None else frozenset()
        changed: list[FormFieldDiff] = []
        for name in sorted(names):
            dump_value = canonical_form_value(dump_form.get(name), name, live_form, evidence)
            live_value = live_form.get(name) if live_form is not None else None
            dump_marker = is_unchecked_marker(dump, page, name, dump_value)
            if dump_value == live_value and dump_value == UNCHECKED:
                # The same string is an off box on one side and literal text on the other. A dump
                # without a text record for the page cannot say, so an unchanged router showing
                # literal text is not a difference against it (the dump predates the record).
                live_marker = is_unchecked_marker(live, page, name, live_value)
                legacy_page = page not in dump.form_unchecked_text
                if dump_marker == live_marker or (legacy_page and dump_marker and not live_marker):
                    continue
            elif dump_value == live_value or legacy_form_spelling(dump_value, live_value, name, evidence):
                continue
            # "<unchecked>" records a box that was off, which a browser expresses by posting
            # nothing. A served page that does not render the control, or renders it disabled,
            # posts nothing for it either: the same state, so not a difference. Literal text
            # that happens to read "<unchecked>" is a real value and is not covered by this.
            if dump_value == UNCHECKED and live_value is None and live_form is not None and dump_marker:
                continue
            changed.append(FormFieldDiff(
                field=name, dump=dump_value, live=live_value, sensitive=name in secrets,
                live_disabled=live_value is None and name in disabled,
                # Tolerated only against a page that rendered a form: one without any control is an
                # unreadable answer, not a firmware that dropped every field.
                live_unrendered=(
                    live_value is None
                    and evidence is not None
                    and bool(evidence.rendered_names)
                    and name not in evidence.rendered_names
                ),
            ))
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
        firmware_changed=bool(
            dump.meta.firmware and live.meta.firmware and dump.meta.firmware != live.meta.firmware
        ),
    )


def is_unchecked_marker(snapshot: Snapshot, page: str, name: str, value: str | None) -> bool:
    """Whether `value` for `page`.`name` is the checkbox/radio off marker: it is the UNCHECKED string
    and the snapshot does not record the field as literal text holding that string."""
    return value == UNCHECKED and name not in snapshot.form_unchecked_text.get(page, ())


def holds_convergence_open(change: FormFieldDiff) -> bool:
    """A dumped form value restore still owes: not a live-only control, not one the live page
    renders disabled (restore reports those as blocked), not one it does not render at all (restore
    notes those and posts the rest of the page)."""
    return change.dump is not None and not change.live_disabled and not change.live_unrendered


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
