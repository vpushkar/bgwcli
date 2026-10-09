"""Restore planning and execution. Ported 1:1 from BGW320-CLI src/restore.ts.

Safety rules carried over verbatim (see the comments at each site):
  - dangerous pages can never appear in a plan;
  - one real Remove per page per run, removes blocked behind same-page adds;
  - prune never releases reservations;
  - forwards whose service is added in the same run are deferred and re-resolved live;
  - reservation follow-up: entry form must belong to the target MAC, the ip must be offered and
    enabled, and a non-IPv4 value (e.g. `normal`) is never posted;
  - Wi-Fi Warning Continue is posted to the owning form's action, not the warning page.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from time import monotonic, sleep
from typing import Any, Literal, Protocol

from .client import NoncePageRefusedError, PostWriteEvidence, RouterResponseError, observe_post, pool_full_metadata
from .errors import (
    RouterAuthError,
    RouterConnectionError,
    RouterSessionPoolFullError,
    SnapshotExtractionError,
    is_page_level_auth_error,
)
from .mutations import DANGEROUS_PAGES, build_submit_plan
from .parser import TruncatedPageError
from .redact import page_sensitive_names, redact_value
from .save_confirmation import save_notification
from .snapshot import (
    FORWARD_COLUMNS,
    IPV4_PATTERN,
    RESERVATION_COLUMNS,
    SERVICE_COLUMNS,
    UNCHECKED,
    Snapshot,
    SnapshotForward,
    SnapshotReservation,
    SnapshotService,
    extract_snapshot,
    find_column,
    missing_form_controls,
    missing_table_structure,
    shows_empty_table_marker,
    truncated_page_problem,
)
from .snapshot_diff import (
    FormFieldDiff,
    SnapshotDiff,
    holds_convergence_open,
    is_unchecked_marker,
    pages_missing_from_dump,
)
from .terminal import sanitize_terminal_text
from .types import ParsedPage
from .write_outcome import WRITE_REJECTED_PREFIX

RestoreStepKind = Literal["add-service", "add-forward", "remove-service", "remove-forward", "reserve", "form", "skip"]
RestoreStepStatus = Literal["applied", "unchanged", "skipped", "blocked", "failed", "not-run", "reconnect-required"]


@dataclass(frozen=True)
class RestoreFollowUp:
    page: str
    select_name: str
    option_value: str
    button: str


# A forward whose custom service is added by an earlier step in the same run. The NAT/Gaming
# dropdown fetched at plan time cannot list that service yet, so the executor re-reads the page
# after the services adds and resolves the payload then (observed live 2026-09-19: the dropdown
# lists custom services as "*<name>" only once they exist).
@dataclass(frozen=True)
class RestoreDeferredForward:
    forward: SnapshotForward
    reason: str


@dataclass(frozen=True)
class RestorePostcondition:
    """Non-secret identity of requested state; form values remain in the existing payload."""
    form_fields: tuple[str, ...] = ()
    unchecked_fields: tuple[str, ...] = ()
    # Posted values for controls the live page rendered disabled at plan time. A disabled control
    # is never read back, so these are checked only when the re-read renders them enabled again.
    if_enabled_fields: tuple[str, ...] = ()
    service: SnapshotService | None = None
    forward: SnapshotForward | None = None
    absent_row: tuple[str, str | None] | None = None


@dataclass(frozen=True)
class RestoreStep:
    order: int
    kind: RestoreStepKind
    page: str
    description: str
    button: str | None = None
    assignments: list[str] | None = None
    display_payload: dict[str, str] | None = None
    raw_payload: dict[str, str] | None = None
    blocked: str | None = None
    follow_up: RestoreFollowUp | None = None
    deferred: RestoreDeferredForward | None = None
    service_name: str | None = None
    warning: str | None = None
    postcondition: RestorePostcondition | None = None
    reconnect_required: bool = False
    lan_address_changed: bool = False
    reconnect_address: str | None = None
    # Names whose values every display path redacts beyond the name rule (password-type controls on
    # the live page or in the dump). Display-only; never part of the --json shape.
    sensitive_names: frozenset[str] = field(default=frozenset(), compare=False, metadata={"serialize": False})


@dataclass(frozen=True)
class RestoreOptions:
    prune: bool = False
    include_secrets: bool = False
    # Restorable page ids selected with `--include`; None plans every section and every form page
    # present in the dump. Sections: services, apphosting (forwards), ipalloc (reservations).
    pages: tuple[str, ...] | None = None


def pending_allocation_requests(
    dump: Snapshot, live_pages: Mapping[str, ParsedPage], pages: Sequence[str] | None = None
) -> list[SnapshotReservation]:
    """Only inspect conflicts for selected reservations that still need a write."""
    if not dump.reservations or (pages is not None and "ipalloc" not in pages):
        return []
    if "ipalloc" not in live_pages:
        raise SnapshotExtractionError("ipalloc: allocation preflight requires the live allocation page")
    live = extract_snapshot({"ipalloc": live_pages["ipalloc"]}, ts="", router_host="")
    current = {r.mac.lower(): r.ip for r in live.reservations}
    return [r for r in dump.reservations if current.get(r.mac.lower()) != r.ip]


# The single shape for the follow-up Save POST, shared by the dry-run printer and the executor.
# At plan time the rendered submit value is unknown (it only appears on the entry page the
# Allocate POST opens), so the printed payload falls back to the button name; the executor always
# substitutes the value the live page renders.
def follow_up_payload(follow_up: RestoreFollowUp, save_value: str | None = None) -> dict[str, str]:
    return {
        follow_up.select_name: follow_up.option_value,
        follow_up.button: save_value if save_value is not None else follow_up.button,
    }


RESTORE_PAGE_ORDER: tuple[str, ...] = (
    "services",
    "apphosting",
    "ipalloc",
    "packetfilter",
    "dosprotect",
    "wconfig",
    "wmacauth",
    "ippass",
    "etherlan",
    # Last on purpose: saving a changed LAN address (ipaddr/ipmask) moves the gateway, which would
    # cut every request that came after it.
    "dhcpserver",
)
FORM_SAVE_BUTTONS: Mapping[str, str] = {
    "dosprotect": "Save",
    "wconfig": "Save",
    "etherlan": "Save",
    "dhcpserver": "Save",
    "ippass": "Save",
    "wmacauth": "Save",
}
# dhcpserver fields whose change moves or renumbers the LAN; the step carries a warning so the
# operator knows to reconnect to the new address (the closing diff will fail to re-fetch).
_LAN_MOVING_FIELDS = ("ipaddr", "ipmask", "dhcp")

_UNORDERED = 0  # placeholder `order` for steps before _push numbers them


def build_restore_plan(
    diff: SnapshotDiff,
    dump: Snapshot,
    live_pages: Mapping[str, ParsedPage],
    options: RestoreOptions,
) -> list[RestoreStep]:
    steps: list[RestoreStep] = []
    # The plan honours the --include selection itself (not only through the diff it was handed), so
    # restore can never write a page the operator deliberately left out.
    selected = options.pages
    missing = set(pages_missing_from_dump(dump, selected))

    def push(step: RestoreStep) -> None:
        if step.page in DANGEROUS_PAGES:
            raise RuntimeError("restore plan touched dangerous page")
        steps.append(replace(step, order=len(steps) + 1))

    replaced: dict[str, SnapshotService] = {}
    for page in RESTORE_PAGE_ORDER:
        if page in missing:
            push(
                RestoreStep(
                    order=_UNORDERED,
                    kind="skip",
                    page=page,
                    description=f"page '{page}' requested with --include but not present in the dump",
                )
            )
            continue
        # Section removes are planned under apphosting; with a selection, services removes still
        # need the apphosting branch to run when only `services` is selected (guarded below).
        if selected is not None and page not in selected and (page != "apphosting" or "services" not in selected):
            continue
        if page == "services":
            # A dumped service whose name the router already uses with other ports/protocol shows up
            # as both missing and extra. Adding it would create a second service with the same name,
            # so it is only ever replaced: with --prune, remove the router's one first, then add it
            # (planned right after that remove below); without --prune the add is blocked.
            live_names = {s.name.casefold() for s in diff.services.extra}
            live_services = live_pages.get("services")
            on_router = {
                s.name.strip().casefold()
                for s in (
                    extract_snapshot({"services": live_services}, ts="", router_host="").services
                    if live_services is not None else ()
                )
            }
            for service in diff.services.missing:
                if service.name.casefold() not in live_names and service.name.strip().casefold() in on_router:
                    push(_blocked(
                        "add-service", "services", _service_description(service),
                        f"a custom service named '{service.name}' is already on the router; restore never "
                        "adds a second service with that name",
                    ))
                elif service.name.casefold() not in live_names:
                    push(_add_service_step(service, live_pages, options))
                elif options.prune:
                    replaced[service.name.casefold()] = service
                else:
                    push(_blocked(
                        "add-service", "services", _service_description(service),
                        f"a custom service with the same name '{service.name}' but other ports or protocol is "
                        "already on the router; restore never adds a second service with that name "
                        "(re-run with --prune to replace it)",
                    ))
            # Service removes are deferred until after the apphosting removes: the gateway silently
            # rejects deleting a custom service that still has a forward (observed live 2026-09-19).
        elif page == "apphosting":
            forwards_selected = selected is None or "apphosting" in selected
            pending_services = {s.service_name or "" for s in steps if s.kind == "add-service" and s.blocked is None}
            if forwards_selected:
                for forward in diff.forwards.missing:
                    if forward.service.casefold() in replaced:
                        # The router's dropdown still offers the service being replaced; a forward
                        # added now would attach to it and keep its remove from going through.
                        push(_blocked(
                            "add-forward", "apphosting",
                            f"add forward {forward.service} -> {forward.device_label} ({forward.device_mac})",
                            f"service '{forward.service}' is replaced in this run (removed, then added again); "
                            "its forwards are added by the next restore run",
                        ))
                        continue
                    push(_forward_step(forward, live_pages, options, pending_services))
            if options.prune:
                if forwards_selected:
                    forward_removes = [
                        _remove_step(page, "remove-forward", f.service, live_pages, options, f.device_label)
                        for f in diff.forwards.extra
                    ]
                    for step in _apply_remove_limit(_block_removes_behind_adds(forward_removes, steps, page)):
                        push(step)
                if selected is None or "services" in selected:
                    service_removes = [
                        _block_service_in_use(
                            _remove_step("services", "remove-service", s.name, live_pages, options),
                            s.name, live_pages.get("apphosting"), steps,
                        )
                        for s in diff.services.extra
                    ]
                    limited = _apply_remove_limit(_block_removes_behind_adds(service_removes, steps, "services"))
                    for extra, step in zip(diff.services.extra, limited, strict=True):
                        push(step)
                        replacement = replaced.get(extra.name.casefold())
                        if replacement is None:
                            continue
                        add = _add_service_step(replacement, live_pages, options)
                        if step.blocked is not None and add.blocked is None:
                            add = _block_step(
                                add, f"replaces the router's service '{extra.name}' (other ports or protocol), "
                                f"whose remove is blocked: {step.blocked}",
                            )
                        push(add)
        elif page == "ipalloc":
            for reservation in diff.reservations.missing:
                push(_reserve_step(reservation, None, live_pages))
            for change in diff.reservations.changed:
                push(_reserve_step(SnapshotReservation(mac=change.mac, ip=change.dump_ip), change.live_ip, live_pages))
        elif page == "packetfilter":
            # Documentary-only note; with an explicit selection (packetfilter is not restorable) it
            # would be noise, so it is only emitted for a full plan.
            if selected is not None:
                continue
            rows = len(dump.tables.get("packetfilter", []))
            push(
                RestoreStep(
                    order=_UNORDERED,
                    kind="skip",
                    page=page,
                    description=f"packet filter rules are documentary only in v1; {rows} rule rows in dump",
                )
            )
        else:
            changes = diff.forms.get(page)
            if not changes:
                continue
            disabled_fields = _disabled_field_names(live_pages.get(page))
            dumped = [c for c in changes if c.dump is not None]
            enabled_changes = [c for c in dumped if c.field not in disabled_fields]
            if not enabled_changes:
                if dumped:
                    names = ", ".join(c.field for c in dumped)
                    push(_blocked(
                        "form", page, f"save {page}",
                        f"the live {page} page renders {names} disabled; the dumped value is posted only "
                        "together with a change to an enabled field on this page, and a disabled control "
                        "does not hold restore convergence open",
                    ))
                continue
            # A control the live page renders disabled is normally left alone. But the dump captured
            # it while it was enabled, and the same save is changing an enabled field on this page —
            # typically the one that re-enables it (wconfig: security11 defwpa -> wpa re-enables
            # key11). The server does not know a field was rendered disabled, so post the dumped
            # value in the same POST; otherwise a factory-reset recovery restores the SSID with the
            # router's default password (observed live 2026-09-21).
            applied = dumped
            # A dump value of UNCHECKED on a checkbox/radio means "this box was off": the browser
            # expresses that by not posting the field at all, so those keys are removed from the
            # payload instead of assigned. The decision is keyed on the LIVE control type, never on
            # the string alone. A dump records the text fields that hold the literal string
            # (`form_unchecked_text`) and those are assigned as-is. Any other "<unchecked>" is the
            # off marker: for a checkable live control it means "off", and it is never posted to
            # anything else. A dump taken before that record existed cannot tell a text value from
            # a marker, so a text control holding the string is skipped with a warning.
            def marker(c: FormFieldDiff, page: str = page) -> bool:
                return is_unchecked_marker(dump, page, c.field, c.dump)

            checkable = _checkable_field_names(live_pages.get(page))
            rendered = _rendered_field_names(live_pages.get(page))
            if page in live_pages:
                # A control the live page does not render at all cannot be set by a browser POST, and
                # its saved state can never be read back. An "<unchecked>" dump value for it needs
                # nothing posted (an absent control is not on); any other value cannot be restored.
                # Either way it is left out of the POST and the postcondition, and the rest of the
                # page is still restored: one field newer firmware dropped must not keep the SSID and
                # key from coming back after a reset.
                absent = [c for c in applied if c.field not in rendered]
                unrestorable = [c.field for c in absent if not marker(c)]
                applied = [c for c in applied if c.field in rendered]
                if unrestorable:
                    names = ", ".join(unrestorable)
                    push(RestoreStep(
                        order=_UNORDERED, kind="skip", page=page,
                        description=(
                            f"the live {page} page does not render {names}; the dumped value cannot be "
                            "restored or verified on this firmware and is left out of the save"
                        ),
                    ))
                    if not applied:
                        continue
                if not applied:
                    names = ", ".join(c.field for c in absent)
                    push(RestoreStep(
                        order=_UNORDERED, kind="skip", page=page,
                        description=f"{names} unchecked in the dump and not rendered by the live page; nothing to post",
                    ))
                    continue

            if page in live_pages:
                # "<unchecked>" records an off box. A rendered control that is not checkable (a text
                # input, a select) cannot be "off": the sentinel is never posted as its value.
                wrong_kind = [c.field for c in applied if marker(c) and c.field not in checkable]
                if wrong_kind:
                    applied = [c for c in applied if c.field not in wrong_kind]
                    names = ", ".join(wrong_kind)
                    push(RestoreStep(
                        order=_UNORDERED, kind="skip", page=page,
                        description=(
                            f"the dump marks {names} unchecked but the live {page} page "
                            "renders it as a control that cannot be unchecked; it is left out of the save"
                        ),
                        warning=(
                            # Names only: the dumped value of a field is never quoted, because the
                            # field may be a secret.
                            f"{names} holds the unchecked-marker text; a dump without a text-field note "
                            "cannot tell it apart from an unchecked control, so that field is left out of the "
                            "save and keeps its live value (the live page renders it as a control that cannot "
                            "be unchecked). If the value is real text, set it by hand or re-dump with this version"
                        ),
                    ))
                    if not applied:
                        continue

            if page == "wmacauth":
                lockout = _mac_filter_lockout(applied, dump, live_pages.get(page))
                if lockout is not None:
                    push(_blocked("form", page, f"save {page}", lockout))
                    continue

            def is_off(c: FormFieldDiff, checkable: set[str] = checkable) -> bool:
                return marker(c) and c.field in checkable

            assignments = [f"{c.field}={c.dump}" for c in applied if not is_off(c)]
            omit = [c.field for c in applied if is_off(c)]
            # The save posts the whole live form, so an unchanged control the dump recorded as a
            # password stays secret even when the live page now renders it as text.
            secret = (
                page_sensitive_names(live_pages.get(page))
                | {c.field for c in changes if c.sensitive}
                | frozenset(dump.form_secrets.get(page, ()))
            )
            def show(name: str, value: str | None, secret: frozenset[str] = secret) -> str:
                return redact_value(name, value if value is not None else "<absent>", options.include_secrets, secret)

            shown = ", ".join(f"{c.field}: {show(c.field, c.live)} -> {show(c.field, c.dump)}" for c in applied)
            step = _without_fields(
                _planned(
                    page,
                    "form",
                    f"save {page}: {shown}",
                    FORM_SAVE_BUTTONS.get(page, "Save"),
                    assignments,
                    live_pages,
                    options,
                ),
                omit,
            )
            step = _with_secrets(step, secret, options.include_secrets)
            step = replace(step, postcondition=RestorePostcondition(
                form_fields=tuple(c.field for c in applied if not is_off(c) and c.field not in disabled_fields),
                unchecked_fields=tuple(omit),
                if_enabled_fields=tuple(c.field for c in applied if not is_off(c) and c.field in disabled_fields),
            ))
            if page == "dhcpserver":
                moving = {c.field: c.dump for c in applied if c.field in _LAN_MOVING_FIELDS}
                if moving:
                    new_ip = moving.get("ipaddr")
                    consequence = (
                        f"the router will move; reconnect to {new_ip} before verifying the whole snapshot"
                        if new_ip else "the connection may be interrupted; verification will use the current endpoint"
                    )
                    step = replace(
                        step,
                        reconnect_required=True,
                        lan_address_changed="ipaddr" in moving,
                        reconnect_address=new_ip or (step.raw_payload or {}).get("ipaddr"),
                        warning=(
                            "this save changes the gateway LAN address/DHCP ("
                            + ", ".join(f"{k}={v}" for k, v in moving.items())
                            + f"); {consequence}"
                        ),
                    )
            push(step)
    return steps


# A restore only ever puts the dump's content onto the router; without --prune it deliberately
# leaves router-only entries in place, so "converged" means everything from the dump is present
# (and, with --prune, that the extras are gone too) — not that the diff is empty.
def restore_converged(after_diff: SnapshotDiff, prune: bool) -> bool:
    if after_diff.services.missing or after_diff.forwards.missing:
        return False
    if after_diff.reservations.missing or after_diff.reservations.changed:
        return False
    # Live-only controls (for example, added by newer firmware) are intentionally preserved by
    # planning. They cannot hold convergence open, even when pruning table entries. Neither can a
    # control the live page renders disabled: its value is never posted or read back, and the plan
    # reports it as a blocked step.
    if any(holds_convergence_open(change) for changes in after_diff.forms.values() for change in changes):
        return False
    if not prune:
        return True
    return not after_diff.services.extra and not after_diff.forwards.extra


# The wmacauth page's per-network mode rows (Radio/Network/Filtering) are derived from the mode
# selects themselves; every other row on that page is the MAC filter list.
_MAC_FILTER_MODE_COLUMNS = frozenset({"Radio", "Network", "Filtering"})


def _mac_filter_rows(rows: Sequence[Mapping[str, str]]) -> set[tuple[tuple[str, str], ...]]:
    return {
        tuple(sorted((k.strip().lower(), v.strip().lower()) for k, v in row.items()))
        for row in rows
        if set(row) != _MAC_FILTER_MODE_COLUMNS
    }


def _mac_filter_lockout(
    applied: Sequence[FormFieldDiff], dump: Snapshot, live: ParsedPage | None
) -> str | None:
    """Why restoring an allow/deny MAC filtering mode now could lock Wi-Fi clients out, or None.

    The filter list is documentary (restore never adds MAC entries). Switching a network to allow or
    deny while the live list lacks the dumped entries admits or refuses the wrong clients, so a
    non-none mode is restored only when every dumped list row is on the live page."""
    modes = [c.field for c in applied if c.field.startswith("wmacr") and c.dump not in (None, "none", UNCHECKED)]
    if not modes:
        return None
    wanted = _mac_filter_rows(dump.tables.get("wmacauth", []))
    present = _mac_filter_rows(live.tables if live is not None else [])
    names = ", ".join(modes)
    if not wanted:
        gap = "the dump does not record the MAC filter list these modes were saved with"
    elif not wanted <= present:
        gap = f"the live MAC filter list lacks {len(wanted - present)} of the {len(wanted)} dumped entries"
    else:
        return None
    return (
        f"restoring the allow/deny MAC filtering mode {names} now could lock Wi-Fi clients out: {gap}; "
        "re-enter the MAC filter list in the gateway UI, then run restore again"
    )


def _with_secrets(step: RestoreStep, secret: frozenset[str], include_secrets: bool) -> RestoreStep:
    """Widen a planned step's redaction to `secret` (the dump may know a password control the live
    page renders as text)."""
    names = step.sensitive_names | secret
    display = step.display_payload
    if display is not None and step.raw_payload is not None:
        display = {
            k: (redact_value(k, step.raw_payload[k], include_secrets, names) if k in step.raw_payload else v)
            for k, v in display.items()
        }
    return replace(step, sensitive_names=names, display_payload=display)


def _without_fields(step: RestoreStep, fields: Sequence[str]) -> RestoreStep:
    if not fields or step.raw_payload is None:
        return step
    raw_payload = {k: v for k, v in step.raw_payload.items() if k not in fields}
    display_payload = {k: v for k, v in (step.display_payload or {}).items() if k not in fields}
    return replace(step, raw_payload=raw_payload, display_payload=display_payload)


def _checkable_field_names(live: ParsedPage | None) -> set[str]:
    if live is None:
        return set()
    return {f.name for f in live.fields if f.type in ("checkbox", "radio")}


def _rendered_field_names(live: ParsedPage | None) -> set[str]:
    """Every named control the live page renders, disabled ones included."""
    if live is None:
        return set()
    return {f.name for f in live.fields} | {s.name for s in live.selects} | {t.name for t in live.textareas}


def _disabled_field_names(live: ParsedPage | None) -> set[str]:
    """Names rendered ONLY as disabled controls. A radio group with one disabled member is still an
    enabled field (the browser posts its enabled members), matching LiveFormEvidence.disabled_names."""
    if live is None:
        return set()
    disabled: set[str] = set()
    enabled: set[str] = set()
    for control in (*live.fields, *live.selects, *live.textareas):
        (disabled if control.disabled else enabled).add(control.name)
    return disabled - enabled


def _blocked(kind: RestoreStepKind, page: str, description: str, reason: str, **extra: Any) -> RestoreStep:
    return RestoreStep(order=_UNORDERED, kind=kind, page=page, description=description, blocked=reason, **extra)


def _service_description(service: SnapshotService) -> str:
    return (
        f"add service {service.name} {service.protocol} {service.ext_min_port}-{service.ext_max_port}"
        f" -> {service.int_start_port}"
    )


def _add_service_step(
    service: SnapshotService, live_pages: Mapping[str, ParsedPage], options: RestoreOptions
) -> RestoreStep:
    description = _service_description(service)
    live = live_pages.get("services")
    if live is None:
        return _blocked("add-service", "services", description, "live page services not fetched")
    # The router's protocol <select> uses option values that differ from the table text
    # (observed 2026-09-19: values both/tcp/udp, labels TCP/UDP, TCP, UDP). Posting the
    # table text ("TCP") is accepted with a 302 but silently dropped, so resolve by label or value.
    protocol_select = next((s for s in live.selects if _normalize(s.name) == "protocol"), None)
    wanted = service.protocol.strip().lower()
    option = None
    if protocol_select is not None:
        option = next(
            (
                o
                for o in protocol_select.option_details or []
                if o.label.strip().lower() == wanted or o.value.strip().lower() == wanted
            ),
            None,
        )
    if option is None or protocol_select is None:
        return _blocked(
            "add-service",
            "services",
            description,
            f"protocol '{service.protocol}' not offered by router protocol dropdown",
        )
    assignments = [
        f"Service={service.name}",
        f"extMinPort={service.ext_min_port}",
        f"extMaxPort={service.ext_max_port}",
        f"intStartPort={service.int_start_port}",
        f"{protocol_select.name}={option.value}",
    ]
    step = _planned("services", "add-service", description, "Add", assignments, live_pages, options)
    return replace(step, service_name=service.name, postcondition=RestorePostcondition(service=service))


def _forward_step(
    forward: SnapshotForward,
    live_pages: Mapping[str, ParsedPage],
    options: RestoreOptions,
    pending_services: set[str] | frozenset[str] = frozenset(),
) -> RestoreStep:
    description = f"add forward {forward.service} -> {forward.device_label} ({forward.device_mac})"
    live = live_pages.get("apphosting")
    if live is None:
        return _blocked("add-forward", "apphosting", description, "live page apphosting not fetched")
    service_select = next((s for s in live.selects if _normalize(s.name) == "service"), None)
    # The gateway lists custom services in the NAT/Gaming dropdown with a leading "*" (value and
    # label, e.g. "*Mosh") and hides services that already have a forward (observed live 2026-09-19).
    wanted_names = {forward.service, f"*{forward.service}"}
    service_option = None
    if service_select is not None:
        service_option = next(
            (
                o
                for o in service_select.option_details or []
                if o.label.strip() in wanted_names or o.value in wanted_names
            ),
            None,
        )
    if service_option is None or service_select is None:
        if forward.service in pending_services:
            return RestoreStep(
                order=_UNORDERED,
                kind="add-forward",
                page="apphosting",
                description=description,
                deferred=RestoreDeferredForward(
                    forward=forward,
                    reason=(
                        f"service '{forward.service}' is added earlier in this run; "
                        "the NAT/Gaming dropdown is re-read after that add"
                    ),
                ),
            )
        return _blocked(
            "add-forward",
            "apphosting",
            description,
            f"service '{forward.service}' not offered by router dropdown "
            "(add the custom service first, or it already has a forward)",
        )
    device_select = next((s for s in live.selects if _normalize(s.name) == "device"), None)
    device_option = None
    if device_select is not None:
        device_option = next(
            (o for o in device_select.option_details or [] if o.value.lower() == forward.device_mac.lower()), None
        )
    if device_option is None or device_select is None:
        return _blocked(
            "add-forward", "apphosting", description, f"device {forward.device_mac} not in router device list"
        )
    # The forwards table shows only the device label. With two devices under one label the saved row
    # cannot be told apart (the save could not be verified, and every later dump or diff of that page
    # fails), so the add waits until the devices are told apart by name.
    label = device_option.label.strip()
    twins = sum(1 for o in device_select.option_details or [] if o.label.strip() == label)
    if twins > 1:
        return _blocked(
            "add-forward", "apphosting", description,
            f"device label '{label}' is shared by {twins} devices in the router's device list; rename one of "
            "them in the gateway UI first",
        )
    step = _planned(
        "apphosting",
        "add-forward",
        description,
        "Add",
        [f"{service_select.name}={service_option.value}", f"{device_select.name}={device_option.value}"],
        live_pages,
        options,
    )

    return replace(step, postcondition=RestorePostcondition(forward=forward))


def _reserve_step(
    reservation: SnapshotReservation, current_ip: str | None, live_pages: Mapping[str, ParsedPage]
) -> RestoreStep:
    mac = reservation.mac.lower()
    description = f"reserve {reservation.ip} for {mac} (currently {current_ip if current_ip is not None else 'DHCP'})"
    live = live_pages.get("ipalloc")
    if live is None:
        return _blocked("reserve", "ipalloc", description, "live page ipalloc not fetched")
    button = next((b for b in live.buttons if b.name.lower() == f"allocate_{mac}"), None)
    if button is None:
        return _blocked("reserve", "ipalloc", description, f"device {mac} not present on IP Allocation page (offline?)")
    if button.disabled:
        return _blocked("reserve", "ipalloc", description, f"the Allocate button for {mac} is disabled")
    # Deliberately NOT routed through _planned()/build_submit_plan: that inherits the page's base
    # payload, and the gateway leaves an "IP Allocation Entry" block rendered for the rest of the
    # web session after any Allocate/Cancel. The base payload would then carry
    # `alloc_<other mac>=normal` ("Address from DHCP pool") and this Allocate POST would release
    # that other device's reservation. Allocate only opens the entry form, so the button alone is
    # the whole payload.
    payload = {button.name: button.value or button.label or "Allocate"}
    return RestoreStep(
        order=_UNORDERED,
        kind="reserve",
        page="ipalloc",
        description=description,
        button=button.name,
        assignments=[],
        display_payload=dict(payload),
        raw_payload=dict(payload),
        follow_up=RestoreFollowUp(
            page="ipalloc", select_name=f"alloc_{mac}", option_value=reservation.ip, button="Save"
        ),
    )


_REMOVE_BUTTON = re.compile(r"^Remove_\d+$")


# Remove_<id> button names are positional against the table rows that carry a name/service value —
# NOT against every row of every >=3-column table on the page (parse_page flattens all such tables
# together, so a second unrelated table would otherwise shift the mapping). We only trust the
# mapping when the counts line up exactly; otherwise we refuse to guess which button goes with
# which row.
def _remove_step(
    page: str,
    kind: RestoreStepKind,
    name: str,
    live_pages: Mapping[str, ParsedPage],
    options: RestoreOptions,
    label: str | None = None,
) -> RestoreStep:
    noun = "service" if kind == "remove-service" else "forward"
    description = f"remove {noun} {name}{f' -> {label}' if label else ''}"
    live = live_pages.get(page)
    if live is None:
        return _blocked(kind, page, description, f"live page {page} not fetched")

    name_aliases = SERVICE_COLUMNS["name"] if kind == "remove-service" else FORWARD_COLUMNS["service"]
    label_aliases = FORWARD_COLUMNS["deviceLabel"] if kind == "remove-forward" else None

    candidate_rows: list[dict[str, str]] = []
    for row in live.tables:
        col = find_column(row, name_aliases)
        if col is not None and row.get(col, "").strip():
            candidate_rows.append(row)

    remove_buttons = sorted(
        (b for b in live.buttons if _REMOVE_BUTTON.match(b.name)),
        key=lambda b: int(b.name[len("Remove_") :]),
    )

    if len(remove_buttons) != len(candidate_rows):
        return _blocked(
            kind,
            page,
            description,
            "cannot map table rows to Remove buttons safely "
            f"({len(candidate_rows)} rows, {len(remove_buttons)} buttons)",
        )

    matching_indexes: list[int] = []
    for index, row in enumerate(candidate_rows):
        name_col = find_column(row, name_aliases)
        assert name_col is not None
        if row.get(name_col, "").strip() != name:
            continue
        if label_aliases is not None:
            label_col = find_column(row, label_aliases)
            if label_col is None or row.get(label_col, "").strip() != label:
                continue
        matching_indexes.append(index)

    if len(matching_indexes) > 1:
        return _blocked(kind, page, description, f"ambiguous: {len(matching_indexes)} rows match '{name}'")
    if not matching_indexes:
        return _blocked(kind, page, description, f"no Remove button found for row '{name}'")

    button = remove_buttons[matching_indexes[0]].name
    step = _planned(page, kind, description, button, [], live_pages, options)
    return replace(step, postcondition=RestorePostcondition(absent_row=(name, label)))


def _planned(
    page: str,
    kind: RestoreStepKind,
    description: str,
    button: str,
    assignments: list[str],
    live_pages: Mapping[str, ParsedPage],
    options: RestoreOptions,
) -> RestoreStep:
    live = live_pages.get(page)
    if live is None:
        return _blocked(
            kind, page, description, f"live page {page} not fetched", button=button, assignments=list(assignments)
        )
    try:
        plan = build_submit_plan(page, live, button, assignments, options.include_secrets)
    except Exception as exc:  # noqa: BLE001 - see comment
        # build_submit_plan raises (rather than returning blocked) when the button itself cannot be
        # found on the live page — e.g. it was renamed by a firmware update. Treat that the same as
        # any other blocked step instead of letting it abort the whole plan.
        return _blocked(kind, page, description, str(exc), button=button, assignments=list(assignments))
    return RestoreStep(
        order=_UNORDERED,
        kind=kind,
        page=page,
        description=description,
        button=button,
        assignments=list(assignments),
        display_payload=plan.display_payload,
        raw_payload=plan.raw_payload,
        blocked=(plan.reason or "blocked") if plan.blocked else None,
        sensitive_names=plan.sensitive_names,
    )


# The gateway silently ignores removing a custom service that still has a NAT/Gaming forward
# (observed live 2026-09-19), and the closing diff would then report a failed step that stops every
# later write. So a service remove is planned only when no live forward uses it, or every forward that
# does is removed by an earlier, unblocked step of this same run.
def _block_service_in_use(
    step: RestoreStep, name: str, apphosting: ParsedPage | None, planned: Sequence[RestoreStep]
) -> RestoreStep:
    if step.blocked is not None:
        return step
    if apphosting is None:
        return _block_step(
            step, "forwards not inspected (live apphosting page not fetched); a service that still has a "
            "forward cannot be removed",
        )
    removed = {
        s.postcondition.absent_row
        for s in planned
        if s.kind == "remove-forward" and s.blocked is None and s.postcondition is not None
    }
    users: list[str] = []
    for row in apphosting.tables:
        service_col = find_column(row, FORWARD_COLUMNS["service"])
        label_col = find_column(row, FORWARD_COLUMNS["deviceLabel"])
        if service_col is None or row.get(service_col, "").strip() != name:
            continue
        label = row.get(label_col, "").strip() if label_col is not None else ""
        if (name, label) not in removed:
            users.append(label or "?")
    if not users:
        return step
    return _block_step(
        step, f"service '{name}' still has {len(users)} NAT/Gaming forward(s) (to {', '.join(users)}) that this "
        "run does not remove; remove the forward first",
    )


# Remove_<id> buttons are resolved once, against the pre-plan snapshot of the live page. An add
# executed earlier in the same run can re-sort or extend that table, which would silently move
# every later Remove_<n> onto a different row. So once a page has at least one add we can
# actually post, every remove on that page waits for a separate --prune run. This runs before
# _apply_remove_limit so a remove blocked here does not burn the single real-remove slot.
def _block_removes_behind_adds(
    remove_steps: list[RestoreStep], planned: Sequence[RestoreStep], page: str
) -> list[RestoreStep]:
    pending_adds = any(
        s.page == page and s.blocked is None and s.kind in ("add-service", "add-forward") for s in planned
    )
    if not pending_adds:
        return remove_steps
    return [
        s
        if s.blocked is not None
        else _block_step(s, "removes on a page with pending adds require a separate --prune run")
        for s in remove_steps
    ]


# A remove that is blocked after its payload was already resolved must not keep that payload: the
# Remove_<n> in it addresses a row position we have just decided we can no longer trust, and a
# stray payload is the only thing execute_restore would need to post it.
def _block_step(step: RestoreStep, reason: str) -> RestoreStep:
    return replace(step, raw_payload=None, display_payload=None, blocked=reason)


# Remove_<id> names are positional; after one removal the rest shift. Allow one real (unblocked)
# remove per page per run — tracked by whether a real remove has actually been emitted yet, not
# by array index, so an unresolvable earlier row doesn't cause a later, genuinely removable row
# to be blocked too.
def _apply_remove_limit(steps: list[RestoreStep]) -> list[RestoreStep]:
    used_real = False
    out: list[RestoreStep] = []
    for step in steps:
        if step.blocked is not None:
            out.append(step)
        elif not used_real:
            used_real = True
            out.append(step)
        else:
            out.append(_block_step(step, "multiple removes on one page require separate --prune runs"))
    return out


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


# --- Execution ------------------------------------------------------------------------------------


class PostResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, Any]: ...

    @property
    def body(self) -> str: ...


class GetResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def body(self) -> str: ...


class RestorePoster(Protocol):
    """The slice of the router client execute_restore needs (client.BGW320Client satisfies it)."""

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> PostResponse: ...

    def post_form(self, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> PostResponse: ...

    def get_cgi_page(self, page: str) -> GetResponse: ...


@dataclass(frozen=True)
class RestoreStepResult:
    order: int
    page: str
    kind: RestoreStepKind
    description: str
    status: RestoreStepStatus
    status_code: int | None = None
    location: str | None = None
    error: str | None = None
    # Reason of a skipped step (also printed by the dry-run plan); never contains a field value.
    warning: str | None = None
    # None means legacy/unknown; only explicit False permits a safe automatic retry.
    write_attempted: bool | None = None
    write_response_received: bool | None = None
    # False is an explicit gateway no-save notification, distinct from a sent request.
    write_performed: bool | None = None
    acknowledgement_observed: bool | None = None
    state_observed: bool | None = None
    reconnect_address: str | None = None
    session_pool_full: bool | None = None
    error_type: str | None = None
    waited_ms: int | None = None
    retry_count: int | None = None
    error_status_code: int | None = None
    lan_address_changed: bool | None = None
    # Configuration POSTs sent for this step's write when a failure leaves its fate in question.
    write_attempts: int | None = None
    # True when the failure is a 401/403 answered by one content page (the session is not known to be
    # lost): callers report the step instead of raising a session-wide authentication error.
    error_page_level: bool = field(default=False, metadata={"serialize": False})
    # True when an acknowledged write is reported as applied without its requested state having been
    # seen: the live re-read decides the exit, whatever the operation.
    reread_required: bool = field(default=False, metadata={"serialize": False})


@dataclass(frozen=True)
class RestoreExecution:
    steps: list[RestoreStepResult] = field(default_factory=list)
    stopped_at: int | None = None


def _parse_page(page: str, body: str) -> ParsedPage:
    # parser.py is owned elsewhere and imported lazily; tests monkeypatch this seam.
    from .parser import parse_page

    return parse_page(page, body, include_secrets=True)


def _location(headers: Mapping[str, Any] | None) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if str(key).lower() == "location":
            if isinstance(value, list | tuple):
                return value[0] if value else None
            return value
    return None


SAVE_CONFIRMATION_TIMEOUT_SECONDS = 60.0
SAVE_CONFIRMATION_POLL_SECONDS = 1.0
# HTTP answers to a verification read (acknowledgement poll or post-save re-read) that are retried:
# request timeout, rate limiting and transient server/gateway errors. Every other status is final.
TRANSIENT_VERIFICATION_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def is_transient_verification_status(status_code: int | None) -> bool:
    """The one retry rule shared by the acknowledgement poll and the CLI's post-save re-read."""
    return status_code in TRANSIENT_VERIFICATION_STATUSES


def router_error_banner(html: str) -> str | None:
    """The gateway reports a rejected form on the redirect target as a banner, not as an HTTP error:
    `<img id="error-message-icon" src="/images/icon_error.png"> <div id="error-message-text"> A required
    setting is empty ...</div>` (observed live 2026-09-21 on NAT/Gaming adds that answered 302). The same
    text element also carries informational messages such as 'Changes saved' — those come WITHOUT the
    error icon and are not errors. Returns the collapsed error text or None."""
    return save_notification("", html).error


def redirect_answer_page(client: Any, location: str | None) -> Any | None:
    """The page a write's 3xx answer redirects to, read once: it carries the gateway's rejection
    banner. None only for a location that is no CGI page. A lost connection, an HTTP error, a
    refused session or a full session pool propagates: the write's answer is then unknown and the
    caller reports it with the write evidence."""
    if not location:
        return None
    match = _CGI_PAGE.search(location)
    if match is None:
        return None
    return client.get_cgi_page(match.group(1))


def _accepted(status_code: int) -> bool:
    return status_code == 200 or 300 <= status_code < 400


def execute_restore(
    client: RestorePoster,
    steps: Sequence[RestoreStep],
    on_step: Callable[[RestoreStepResult], None] | None = None,
) -> RestoreExecution:
    results: list[RestoreStepResult] = []
    stopped_at: int | None = None
    for step in sorted(steps, key=lambda s: s.order):
        base = {"order": step.order, "page": step.page, "kind": step.kind, "description": step.description,
                "write_attempted": False}
        if stopped_at is not None:
            result = RestoreStepResult(**base, status="not-run")
        elif step.kind == "skip":
            result = RestoreStepResult(**base, status="skipped", warning=step.warning)
        elif step.deferred is not None:
            result = _run_deferred_forward(client, step, base)
        elif step.blocked is not None or step.raw_payload is None:
            result = RestoreStepResult(**base, status="blocked", error=step.blocked or "no payload")
        else:
            try:
                # Allocate opens an editor; every other POST could be the final write.
                base["write_attempted"] = step.follow_up is None
                response = _post_with_write_evidence(client, step.page, step.raw_payload, base)
                location = _location(response.headers)
                if not _accepted(response.status_code):
                    result = RestoreStepResult(
                        **base, status="failed", status_code=response.status_code, location=location,
                        error=write_guidance(f"unexpected HTTP {response.status_code}", base),
                    )
                elif step.follow_up is not None:
                    result = _run_follow_up(client, step.follow_up, base)
                elif location and _CONFIRMATION_PAGE.search(location):
                    # The gateway explicitly requires Continue before committing this Save. The
                    # save POST's answer is how far the write got: every failure produced while
                    # confirming it (warning GET, missing Continue, Continue POST) keeps it.
                    base["status_code"], base["location"] = response.status_code, location
                    result = _confirm_warning_page(client, location, base, step=step)
                else:
                    result = _wait_for_save(client, step.page, response, base, step=step)
            except Exception as exc:  # noqa: BLE001 - preserve execution evidence on every failure
                if (step.reconnect_required and isinstance(exc, RouterConnectionError)
                        and base["write_attempted"] is not False):
                    result = _reconnect_result(base, step, cause=exc)
                else:
                    result = _execution_error(base, exc)
        if result.status in {"failed", "reconnect-required"}:
            stopped_at = step.order
        results.append(result)
        if on_step is not None:
            on_step(result)
    return RestoreExecution(steps=results, stopped_at=stopped_at)


def _post_with_write_evidence(
    client: RestorePoster,
    page: str,
    payload: Mapping[str, str],
    base: dict[str, Any],
    *,
    nonce_page: str | None = None,
) -> PostResponse:
    """Use optional public transport evidence without guessing legacy poster delivery. With
    `nonce_page` the POST goes to `page` carrying the nonce of the form `nonce_page` serves for it."""
    final_write = base.get("write_attempted") is not False
    evidence = PostWriteEvidence()
    try:
        with observe_post(client, evidence):
            if nonce_page is None:
                response = client.post_cgi_page(page, payload)
            else:
                response = client.post_form(nonce_page, f"{page}.ha", payload)
            evidence.attempted = evidence.response_received = True
        # The observation closes with the block above: only now is the attempt count known.
        if final_write and evidence.attempts is not None and evidence.attempts > 1:
            # A Login-page answer re-sent the write once and the re-send was answered: the result
            # still says the change went out twice.
            base["write_attempts"] = max(base.get("write_attempts") or 0, evidence.attempts)
        return response
    except Exception:
        # A failure leaves the transport's own account of the write: how many configuration POSTs
        # went out (a Login-page answer re-sends once) and the status the last answered one carried.
        # A later GET's HTTP error is never the POST's status.
        if final_write:
            if evidence.attempts is not None:
                # The most POSTs any one request of the step needed (a re-sent Save stays counted
                # when its single Continue then fails).
                base["write_attempts"] = max(base.get("write_attempts") or 0, evidence.attempts)
            if evidence.response_received and evidence.status_code is not None and base.get("status_code") is None:
                base["status_code"] = evidence.status_code  # an earlier Save answer (Continue path) stays
        raise
    finally:
        if final_write:
            base["write_attempted"] = evidence.attempted
            base["write_response_received"] = evidence.response_received


# The one guidance sentence every write path ends an uncertain outcome with.
VERIFY_BEFORE_RETRYING = "verify the gateway state before retrying."
SENT_ONCE_GUIDANCE = f"The change was sent once; {VERIFY_BEFORE_RETRYING}"


def sent_guidance(attempts: int | None) -> str:
    """The "sent once" guidance sentence, or "sent N times" when the transport re-sent the write."""
    if attempts is not None and attempts > 1:
        return (
            f"The change was sent {attempts} times (a Login-page answer forces one re-login and "
            f"re-send); {VERIFY_BEFORE_RETRYING}"
        )
    return SENT_ONCE_GUIDANCE


def append_sentence(message: str, sentence: str) -> str:
    """`message` + `sentence` as two sentences, without doubling a period `message` already ends in."""
    return f"{message[:-1] if message.endswith('.') else message}. {sentence}"


def write_guidance(
    message: str, base: Mapping[str, Any], *, sent: bool = True, sent_text: str | None = None
) -> str:
    attempts = base.get("write_attempts")
    if base.get("write_attempted") and attempts is not None and attempts > 1:
        # The one documented re-send after a Login-page answer happened.
        answered = "the last one was answered" if base.get("write_response_received") else "the last one got no answer"
        return append_sentence(
            message,
            f"The change was sent {attempts} times (a Login-page answer forces one re-login and "
            f"re-send) and {answered}; {VERIFY_BEFORE_RETRYING}",
        )
    if base.get("write_attempted"):
        evidence = sent_text or ("The change was sent once" if sent
                                 else "The final write was attempted once and may have reached the gateway")
        return append_sentence(message, f"{evidence}; {VERIFY_BEFORE_RETRYING}")
    if base.get("write_attempted") is None:
        return append_sentence(message, f"Final write delivery is unknown; {VERIFY_BEFORE_RETRYING}")
    return message


def _execution_error(
    base: dict[str, Any], exc: Exception, *, sent: bool = False, note: str | None = None,
    sent_text: str | None = None,
) -> RestoreStepResult:
    waited_ms, retry_count = pool_full_metadata(exc)
    message = append_sentence(str(exc), note) if note else str(exc)
    if isinstance(exc, NoncePageRefusedError) and base.get("write_response_received") is not True:
        # A refused nonce page proves nothing was performed, unless an earlier POST of the step (the
        # Save that led to the Wi-Fi Warning) was already answered: then only Continue was refused.
        base = {**base, "write_performed": False}
    return RestoreStepResult(
        **base, status="failed", error=write_guidance(
            message, base, sent=sent or base.get("write_response_received") is True, sent_text=sent_text,
        ),
        session_pool_full=isinstance(exc, RouterSessionPoolFullError), error_type=type(exc).__name__,
        waited_ms=waited_ms if isinstance(exc, RouterSessionPoolFullError) else None,
        retry_count=retry_count if isinstance(exc, RouterSessionPoolFullError) else None,
        error_status_code=getattr(exc, "status_code", None),
        error_page_level=is_page_level_auth_error(exc),
    )


def reconnect_verification_reason(step: RestoreStepResult) -> str:
    if step.error:
        return step.error
    address = step.reconnect_address or "the new LAN address"
    return f"LAN address changed; reconnect to {address}; whole-snapshot verification unavailable at the old endpoint"


def _reconnect_result(
    base: dict[str, Any], step: RestoreStep, *, cause: Exception | None = None, **evidence: Any
) -> RestoreStepResult:
    if cause is not None:
        failure = _execution_error(base, cause)
        evidence.update(error_type=failure.error_type, error_status_code=failure.error_status_code,
                        session_pool_full=failure.session_pool_full, waited_ms=failure.waited_ms,
                        retry_count=failure.retry_count, error_page_level=failure.error_page_level)
    address = f" at {step.reconnect_address}" if step.reconnect_address else " using the new LAN settings"
    message = f"LAN settings may have changed; reconnect{address}; final verification unavailable"
    if cause is not None:
        message = append_sentence(str(cause), message)
    return RestoreStepResult(
        **base, **evidence, status="reconnect-required", reconnect_address=step.reconnect_address,
        error=write_guidance(
            message, base,
            sent=evidence.get("status_code") is not None or base.get("write_response_received") is True,
        ),
    )


# Re-resolve a deferred forward against a fresh apphosting page. The service add that made it
# possible ran earlier in this same execution (or execution would already have stopped), so the
# dropdown is the only thing that can still say no — in which case the step stays blocked with
# the dropdown's answer, and the convergence diff reports the forward as missing.
def _run_deferred_forward(client: RestorePoster, step: RestoreStep, base: dict[str, Any]) -> RestoreStepResult:
    deferred = step.deferred
    assert deferred is not None
    # Everything here runs AFTER earlier steps have already written to the router, so nothing may
    # escape: a thrown GET (timeout, connection reset) must become a failed step result, or the
    # partial execution report — including the service add that just succeeded — would be lost.
    try:
        page = client.get_cgi_page("apphosting")
        if page.status_code != 200:
            return RestoreStepResult(
                **base, status="failed", error=f"unexpected HTTP {page.status_code} re-reading apphosting"
            )
        parsed = _parse_page("apphosting", page.body)
        # A dropdown can only say "no" on a readable form: an unreadable answer is a failed read.
        problem = _unreadable_form_problem("apphosting", page.body, parsed)
        if problem is not None:
            # A structural read failure keeps its exception type, so restore exits 2 and autorestore ends
            # the run instead of replanning from a stale snapshot. Nothing was sent for this step.
            return _execution_error(base, SnapshotExtractionError(f"re-reading apphosting: {problem}"))
        resolved = _forward_step(deferred.forward, {"apphosting": parsed}, RestoreOptions())
        if resolved.blocked is not None or resolved.raw_payload is None:
            return RestoreStepResult(
                **base, status="blocked", error=resolved.blocked or "no payload after re-reading apphosting"
            )
        base["write_attempted"] = True
        response = _post_with_write_evidence(client, "apphosting", resolved.raw_payload, base)
        location = _location(response.headers)
        if _accepted(response.status_code):
            return _wait_for_save(client, "apphosting", response, base, step=resolved)
        return RestoreStepResult(
            **base,
            status="failed",
            status_code=response.status_code,
            location=location,
            error=write_guidance(f"unexpected HTTP {response.status_code}", base),
        )
    except Exception as exc:  # noqa: BLE001
        return _execution_error(base, exc)


# Advanced Wi-Fi saves answer 302 -> /cgi-bin/wifiwarn_advanced.ha, a "Wi-Fi Warning" page with
# Continue/Cancel; without Continue the change is discarded (observed live 2026-09-19).
_CONFIRMATION_PAGE = re.compile(r"/cgi-bin/wifiwarn[a-z_]*\.ha", re.IGNORECASE)
_CGI_PAGE = re.compile(r"/cgi-bin/([a-z0-9_]+)\.ha", re.IGNORECASE)


# The only Continue form actions that resolve: the client's `_form_target` rule (the part after
# `/cgi-bin/`) kept to a rooted `/cgi-bin/<page>.ha` or a relative `<page>.ha`, with no query.
_CONTINUE_TARGET = re.compile(r"(?:/cgi-bin/)?([a-z0-9_]+)\.ha")

CONTINUE_NOT_POSTED = "The Wi-Fi Warning was not confirmed: Continue was not posted"


def _confirm_warning_page(
    client: RestorePoster, location: str, base: dict[str, Any], *, step: RestoreStep | None = None
) -> RestoreStepResult:
    """Confirm the Wi-Fi Warning the save POST redirected to. That POST was sent and answered (the
    redirect is its answer), so the step reports a sent write on every outcome; a failure before
    Continue leaves says so in the error. Once Continue is sent, its delivery is the final write."""
    base.setdefault("location", location)
    base["write_attempted"] = True
    base["write_response_received"] = True

    def not_confirmed(message: str) -> RestoreStepResult:
        return RestoreStepResult(
            **base, status="failed", error=write_guidance(append_sentence(message, CONTINUE_NOT_POSTED), base)
        )

    match = _CGI_PAGE.search(location)
    if match is None:
        return not_confirmed(f"cannot derive confirmation page from {location}")
    page = match.group(1)
    try:
        warn = client.get_cgi_page(page)
    except Exception as exc:  # noqa: BLE001 - keeps the save POST's evidence; callers re-raise coordination
        return _execution_error(base, exc, note=CONTINUE_NOT_POSTED)
    if warn.status_code != 200:
        return not_confirmed(f"unexpected HTTP {warn.status_code} reading {page}")
    parsed = _parse_page(page, warn.body)
    cont = next((b for b in parsed.buttons if _normalize(b.name) == "continue" and not b.disabled), None)
    if cont is None:
        return not_confirmed(f"{page} has no Continue button; change not confirmed")
    # The Continue button lives in a form whose action is the ORIGINAL page (wconfig.ha), not the
    # warning page; posting it to the warning page is accepted (302) but discards the change, so a
    # form that cannot be resolved exactly is refused instead of being posted to the warning page.
    owning_form = next((f for f in parsed.forms if cont.name in f.button_names), None)
    action = owning_form.action.strip() if owning_form is not None else ""
    target = _CONTINUE_TARGET.fullmatch(action)
    if target is None:
        shown = sanitize_terminal_text(action, single_line=True)[:200] if action else "no action"
        return not_confirmed(
            f"{page}: the Continue button's form posts to {shown}, not to a page this tool can confirm, "
            "so the change is not confirmed and the gateway discards an unconfirmed Wi-Fi change"
        )
    target_page = target.group(1)
    try:
        # Continue carries the nonce of the warning page's own Continue form (never the target page's).
        response = _post_with_write_evidence(
            client,
            target_page,
            {cont.name: cont.value or cont.label or cont.name},
            base,
            nonce_page=page,
        )
    except Exception as exc:  # noqa: BLE001 - the save POST stays sent whatever happened to Continue
        continue_sent = base["write_attempted"] is not False
        base["write_attempted"] = True
        if not continue_sent:
            base["write_response_received"] = True
            if isinstance(exc, NoncePageRefusedError):
                # The client's refusal reason stays, but its "nothing was sent" clause is untrue here:
                # the Save POST was answered. Say what is actually pending instead.
                reason = str(exc).replace("; nothing was sent", "")
                return _execution_error(
                    base,
                    NoncePageRefusedError(reason),
                    note=f"{CONTINUE_NOT_POSTED}, so the change is not confirmed and the gateway discards an "
                    "unconfirmed Wi-Fi change",
                )
            return _execution_error(base, exc, note=CONTINUE_NOT_POSTED)
        # Both POSTs went out: the Save was answered (302 to the Wi-Fi Warning) and the Continue POST
        # then failed or got no answer. The result's statusCode stays the Save's 302.
        return _execution_error(
            base, exc,
            note="The Save POST was answered 302 and the Continue POST failed or got no answer",
            sent_text="Two POSTs went out (the Save and the Continue)",
        )
    final_location = _location(response.headers)
    if _accepted(response.status_code):
        return _wait_for_save(client, target_page, response, base, step=step)
    return RestoreStepResult(
        **{**base, "status_code": response.status_code, "location": final_location},
        status="failed",
        error=write_guidance(f"unexpected HTTP {response.status_code} confirming {target_page}", base),
    )


def _run_follow_up(client: RestorePoster, follow_up: RestoreFollowUp, base: dict[str, Any]) -> RestoreStepResult:
    page = client.get_cgi_page(follow_up.page)
    if page.status_code != 200:
        return RestoreStepResult(
            **base, status="failed", error=f"unexpected HTTP {page.status_code} reading {follow_up.page}"
        )
    # Second layer of the release guard (the first is read_dump_file): the entry form always offers
    # `normal` ("Address from DHCP pool") as a live, non-disabled option, so anything that is not a
    # literal IPv4 address would be accepted by the option lookup below and release the reservation.
    # Every refusal below happens before the Save POST: nothing was written (the Allocate POST only
    # opens the entry editor), so the step is `blocked` and the rest of the plan still runs.
    if not IPV4_PATTERN.match(follow_up.option_value):
        return RestoreStepResult(
            **base, status="blocked", error=f"refusing to post non-IPv4 allocation value '{follow_up.option_value}'"
        )
    parsed = _parse_page(follow_up.page, page.body)
    select = next((s for s in parsed.selects if s.name.lower() == follow_up.select_name.lower()), None)
    mac = re.sub(r"^alloc_", "", follow_up.select_name, flags=re.IGNORECASE)
    if select is None:
        return RestoreStepResult(**base, status="blocked", error=f"IP Allocation Entry did not open for {mac}")
    option = next(
        (o for o in select.option_details or [] if o.value == follow_up.option_value and not o.disabled), None
    )
    if option is None:
        return RestoreStepResult(
            **base,
            status="blocked",
            error=f"address {follow_up.option_value} not offered (in use by another device?)",
        )
    # The gateway drops POSTs whose submit value does not match the rendered control, so post the
    # value the entry page actually renders (observed "Save" here, "Save..." on wconfig) rather
    # than echoing the control's name.
    save_button = next((b for b in parsed.buttons if b.name == follow_up.button), None)
    if save_button is None:
        return RestoreStepResult(
            **base, status="blocked", error=f"IP Allocation Entry has no '{follow_up.button}' button"
        )
    if save_button.disabled:
        return RestoreStepResult(
            **base, status="blocked", error=f"the IP Allocation Entry '{follow_up.button}' button is disabled"
        )
    base["write_attempted"] = True
    response = _post_with_write_evidence(
        client, follow_up.page,
        follow_up_payload(follow_up, save_button.value or save_button.label or save_button.name), base,
    )
    location = _location(response.headers)
    if _accepted(response.status_code):
        return _wait_for_save(
            client, follow_up.page, response, base,
            reservations=[SnapshotReservation(mac=mac, ip=follow_up.option_value)],
        )
    return RestoreStepResult(
        **base,
        status="failed",
        status_code=response.status_code,
        location=location,
        error=write_guidance(f"unexpected HTTP {response.status_code}", base),
    )


def _unreadable_form_problem(page: str, html: str, parsed: ParsedPage) -> str | None:
    """Why a read of `page` cannot be taken as its form (truncated, control-less, Login page, Page not
    found), or None for a readable form. An absent control or dropdown entry is evidence only on a
    readable form; on any of these answers it is a failed read."""
    from .fetch import is_page_not_found
    from .parser import looks_like_login

    return (
        truncated_page_problem(page, parsed)
        or missing_form_controls(page, parsed, any_page=True)
        or ("the page answered with the Login page" if looks_like_login(html) else None)
        or ("the page answered Page not found" if is_page_not_found(parsed) else None)
    )


def _unreadable_verification_problem(page: str, html: str, parsed: ParsedPage) -> str | None:
    """Why a verification read of `page` proves nothing about its state: not a readable page (see
    `_unreadable_form_problem`), or, for the table pages (services, apphosting, ipalloc), a page that shows
    neither its table header row nor the gateway's one-cell empty table. Such a body extracts as an EMPTY
    table, and a row absent from an empty table is not evidence that the row is gone or was never added."""
    if page not in ("services", "apphosting", "ipalloc"):
        return _unreadable_form_problem(page, html, parsed)
    # A table page is readable when it shows its table header or the gateway's "No ... entries have been
    # defined" cell: that, not a form control, is what a "Please wait" or cut-off answer lacks (a header-only
    # table and that one-cell table are both a legitimate empty section).
    from .fetch import is_page_not_found
    from .parser import looks_like_login

    return (
        truncated_page_problem(page, parsed)
        or ("the page answered with the Login page" if looks_like_login(html) else None)
        or ("the page answered Page not found" if is_page_not_found(parsed) else None)
        or missing_table_structure(page, parsed, html)
    )


def _postcondition_matches(step: RestoreStep, html: str) -> bool:
    wanted = step.postcondition
    assert wanted is not None
    parsed = _parse_page(step.page, html)
    # Every kind answers yes/no only from a readable page: a "Please wait" document, a page without its
    # form controls (form pages) or without its table header row or the gateway's empty-table cell (services,
    # apphosting, ipalloc), a Login page or a Page-not-found answer is a failed read, never an absent row/box.
    problem = _unreadable_verification_problem(step.page, html, parsed)
    if problem is not None:
        raise SnapshotExtractionError(problem)
    # Unified Wi-Fi is a CLI form, not a separate dump section; use the same form extractor.
    snapshot_page = "wconfig" if step.page == "wconfig_unified" else step.page
    snapshot = extract_snapshot({snapshot_page: parsed}, ts="", router_host="", include=(snapshot_page,))
    if wanted.service is not None:
        return wanted.service in snapshot.services
    if wanted.forward is not None:
        return any(f.service == wanted.forward.service and f.device_mac.lower() == wanted.forward.device_mac.lower()
                   for f in snapshot.forwards)
    if wanted.absent_row is not None:
        # Only a recognizable configuration table can prove absence; an unrecognizable one proves
        # nothing, so it is an unreadable verification, never evidence that the row is still there.
        from .parser import parse_document

        aliases = SERVICE_COLUMNS if step.kind == "remove-service" else FORWARD_COLUMNS
        required = ("name", "extMinPort", "intStartPort", "protocol") if step.kind == "remove-service" else (
            "service", "deviceLabel"
        )
        # The gateway's one-cell "No ... entries have been defined" table (no header row) is the section
        # with no entries at all, so the row is gone: the shape the LAST remove on a page reads back.
        recognized = shows_empty_table_marker(step.page, html)
        for table in () if recognized else parse_document(html).find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headings = {cell.text(): "" for cell in rows[0].find_all("th") + rows[0].find_all("td")}
            if all(find_column(headings, aliases[key]) is not None for key in required):
                recognized = True
                break
        if not recognized:
            kind_label = "custom services" if step.kind == "remove-service" else "port forwards"
            raise SnapshotExtractionError(
                f"the {step.page} page shows no {kind_label} table with the columns needed to prove a removed row "
                "is gone; it is not read as a row still present"
            )
        name, label = wanted.absent_row
        if step.kind == "remove-service":
            return not any(s.name == name for s in snapshot.services)
        return not any(f.service == name and f.device_label == label for f in snapshot.forwards)
    live = snapshot.forms.get(snapshot_page, {})
    posted = step.raw_payload or {}
    # The form extractor omits disabled controls, so a name missing here was not rendered enabled:
    # a box the save disabled (or stopped rendering) is not on, which is what "unchecked" asks.
    return (
        all(live.get(name) == posted.get(name) for name in wanted.form_fields)
        and all(live.get(name, UNCHECKED) == UNCHECKED for name in wanted.unchecked_fields)
        and all(name not in live or live[name] == posted.get(name) for name in wanted.if_enabled_fields)
    )


@dataclass
class _SaveEvidence:
    saved: bool = False
    no_changes: bool = False
    state: bool | None = None
    state_unreadable: bool = False
    # The structural failure of the last unreadable poll, kept so an acknowledged save that stays
    # unverifiable to the deadline reports it (error type included); a readable poll clears it.
    state_problem: SnapshotExtractionError | None = None
    rejection: str | None = None
    read_error: Exception | None = None
    timed_out: bool = False


def _save_pending_reason(observed: _SaveEvidence) -> str:
    if observed.read_error is not None:
        if isinstance(observed.read_error, RouterResponseError):
            return f"verification read returned HTTP {observed.read_error.status_code}"
        return "verification read failed"
    if observed.saved:
        state = "could not be read" if observed.state_unreadable else "is not visible"
        return f"Changes saved was observed but the requested state {state}"
    reason = "Changes saved was not observed"
    if observed.state_unreadable:
        reason += "; the requested state could not be read"
    return reason


def _save_outcome(
    base: dict[str, Any], step: RestoreStep | None, observed: _SaveEvidence, *,
    known_state: bool, status_code: int, location: str | None, page_name: str, allocation_target: str = "",
) -> RestoreStepResult:
    """Decide once from observed facts; an acknowledgement deadline is not transport loss."""
    # The final write's own answer is the evidence here; drop any earlier (save POST) status/location.
    base = {k: v for k, v in base.items() if k not in ("status_code", "location")}
    base = {**base, "write_performed": True if observed.saved else False if observed.no_changes else None}
    evidence = {"status_code": status_code, "location": location,
                "acknowledgement_observed": observed.saved, "state_observed": observed.state}
    matched = not known_state or observed.state is True
    if observed.rejection:
        return RestoreStepResult(
            **base, **evidence, status="failed",
            error=write_guidance(f"{WRITE_REJECTED_PREFIX}{observed.rejection}", base),
        )
    if observed.saved and matched:
        return RestoreStepResult(
            **base, **evidence, status="applied",
            lan_address_changed=True if step is not None and step.lan_address_changed else None,
            reconnect_address=step.reconnect_address if step is not None and step.lan_address_changed else None,
        )
    if observed.no_changes and not observed.saved:
        return RestoreStepResult(
            **base, **evidence, status="unchanged" if matched else "failed",
            error=None if matched else (
                "No changes detected. Save not performed. The requested state could not be verified; "
                f"{VERIFY_BEFORE_RETRYING}"
            ),
        )
    if step is not None and (step.lan_address_changed or (
        step.reconnect_required and isinstance(observed.read_error, RouterConnectionError)
    )):
        return _reconnect_result(base, step, cause=observed.read_error, **evidence)
    deadline_message = (
        f"Timed out waiting for Changes saved{allocation_target} on {page_name}: {_save_pending_reason(observed)}"
    )
    if observed.read_error is not None:
        failure = replace(_execution_error(base, observed.read_error, sent=True), **evidence)
        if observed.timed_out:
            failure = replace(failure, error=write_guidance(f"{deadline_message}: {observed.read_error}", base))
        return failure
    if observed.saved and observed.state is not True and observed.state_unreadable and observed.state_problem:
        # Acknowledged, but the verification page never became readable: no answer about the state, so
        # the step keeps the structural exception type (and the acknowledgement/performed evidence).
        failure = replace(_execution_error(base, observed.state_problem, sent=True), **evidence)
        return replace(failure, error=write_guidance(deadline_message, base))
    return RestoreStepResult(**base, **evidence, status="failed", error=write_guidance(deadline_message, base))


def _wait_for_save(
    client: RestorePoster,
    page_name: str,
    response: PostResponse,
    base: dict[str, Any],
    *,
    reservations: Sequence[SnapshotReservation] = (),
    releases: Sequence[str] = (),
    step: RestoreStep | None = None,
) -> RestoreStepResult:
    """Collect acknowledgement, state, and real failures without repeating a write."""
    base = {**base, "write_attempted": True, "write_response_received": True}
    location = _location(response.headers)
    deadline = monotonic() + SAVE_CONFIRMATION_TIMEOUT_SECONDS
    redirect = _CGI_PAGE.search(location or "") if 300 <= response.status_code < 400 else None
    first_read_page = redirect.group(1) if redirect is not None else page_name
    known_state = bool(reservations) or bool(releases) or (
        step is not None and (step.postcondition is not None or step.reconnect_required)
    )
    html = response.body
    observed = _SaveEvidence()
    polled = False
    while True:
        notification = None
        if html:
            try:
                notification = save_notification(page_name, html)
            except TruncatedPageError as exc:
                # A body cut by the parser's bounds is an unreadable poll, never a lost step: the POST was
                # answered, so the wait keeps polling. Before an acknowledgement it teaches nothing about the
                # save either (exactly like a Please-wait body), so the deadline reports the not-acknowledged
                # shape with the write evidence; once "Changes saved" was observed the write happened and
                # the cut poll only leaves the state unreadable. Without a tracked state the cut body is
                # exactly a Please-wait body: no state read was attempted, so none is reported unreadable.
                observed.state = None
                if known_state:
                    observed.state_unreadable = True
                    observed.state_problem = SnapshotExtractionError(str(exc))
        if notification is not None:
            observed.saved |= notification.saved
            observed.no_changes |= notification.no_changes
            if notification.error:
                observed.rejection = notification.error
                break
            try:
                if reservations or releases:
                    parsed = _parse_page(page_name, html)
                    problem = _unreadable_verification_problem(page_name, html, parsed)
                    if problem is not None:
                        raise SnapshotExtractionError(problem)
                    snapshot = extract_snapshot({page_name: parsed}, ts="", router_host="")
                    fixed = {r.mac.lower(): r.ip for r in snapshot.reservations}
                    # A release is seen when the device is listed but no longer a Fixed Allocation row;
                    # a device with no row at all is not seen (the re-read reports it unconfirmable).
                    listed = {
                        row[key].strip().lower() for row in parsed.tables
                        if (key := find_column(row, RESERVATION_COLUMNS["mac"])) is not None
                    }
                    observed.state = (
                        all(fixed.get(w.mac.lower()) == w.ip for w in reservations)
                        and all(mac not in fixed and mac in listed for mac in releases)
                    )
                elif step is not None and step.postcondition is not None:
                    observed.state = _postcondition_matches(step, html)
            except SnapshotExtractionError as exc:
                observed.state = None
                observed.state_unreadable = True
                observed.state_problem = exc
            else:
                observed.state_unreadable = False
                observed.state_problem = None
            if observed.saved and (not known_state or observed.state is True):
                break
            if observed.no_changes and not observed.saved:
                break
        # A known address move makes reads at the old endpoint unsafe. Mask/DHCP
        # changes alone do not establish a move, so those keep verifying here.
        if step is not None and step.lan_address_changed:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            observed.timed_out = True
            break
        if polled:
            sleep(min(SAVE_CONFIRMATION_POLL_SECONDS, remaining))
            if monotonic() >= deadline:
                observed.timed_out = True
                break
        read_page = page_name if polled else first_read_page
        polled = True
        html = ""
        try:
            page = client.get_cgi_page(read_page)
            if page.status_code != 200:
                raise RouterResponseError(
                    f"unexpected HTTP {page.status_code} confirming the change on {page_name}",
                    status_code=page.status_code,
                )
        except RouterConnectionError as exc:
            observed.read_error = exc
            if step is not None and step.reconnect_required:
                break
        except RouterResponseError as exc:
            observed.read_error = exc
            if not is_transient_verification_status(exc.status_code):
                break
        except RouterAuthError as exc:
            # Final either way: a page-level 401/403 is reported on the step (error_page_level); a lost
            # session or a full pool is re-raised by the caller with the write evidence in its text.
            observed.read_error = exc
            break
        except Exception as exc:  # noqa: BLE001 - retain write and pool coordination evidence
            observed.read_error = exc
            break
        else:
            observed.read_error = None
            html = page.body
    return _save_outcome(
        base, step, observed, known_state=known_state, status_code=response.status_code,
        location=location, page_name=page_name,
        # The wait names what it waited for: a reservation's row, or a release (the row no longer Fixed).
        allocation_target=(
            " and fixed allocation" if reservations else " and allocation release" if releases else ""
        ),
    )


def confirm_saved_page(
    client: RestorePoster, page: str, response: PostResponse, *, payload: Mapping[str, str] | None = None,
    write_attempts: int | None = None,
) -> RestoreStepResult:
    """Shared acknowledgement wait for configuration writes from set and submit. `write_attempts` is
    the POST count of a write the transport re-sent after a Login-page answer (None for one POST):
    every result of the wait carries it and its guidance says how many times the change was sent."""
    base: dict[str, Any] = {"order": 0, "page": page, "kind": "form", "description": f"confirm saved {page}"}
    if write_attempts is not None:
        base["write_attempts"] = write_attempts
    reservations = [
        SnapshotReservation(mac=name[len("alloc_"):], ip=value)
        for name, value in (payload or {}).items()
        if page == "ipalloc" and name.lower().startswith("alloc_") and IPV4_PATTERN.fullmatch(value)
    ]
    releases = [
        name[len("alloc_"):].lower()
        for name, value in (payload or {}).items()
        if page == "ipalloc" and name.lower().startswith("alloc_") and not IPV4_PATTERN.fullmatch(value)
    ]
    return _wait_for_save(client, page, response, base, reservations=reservations, releases=releases)


def is_confirmation_redirect(location: str | None) -> bool:
    """True when a POST answered with a redirect to the gateway's Wi-Fi Warning confirmation page."""
    return bool(location) and _CONFIRMATION_PAGE.search(location) is not None


def confirm_warning_page(
    client: RestorePoster, location: str, *, status_code: int | None = None, page: str = "wconfig",
    write_attempts: int | None = None,
) -> RestoreStepResult:
    """Follow a Wi-Fi Warning redirect and post Continue to the owning form (shared by set and restore).
    `status_code` is the save POST's answer, kept on every failure produced while confirming; `page` is
    the owning form's page (wconfig or wconfig_unified) named by the step."""
    base = {"order": 0, "page": page, "kind": "form", "description": "confirm Wi-Fi Warning",
            "write_attempted": True, "write_response_received": True, "status_code": status_code,
            "location": location}
    if write_attempts is not None:
        # The Save was re-sent: every text built below says so, combined with Continue's own count.
        base["write_attempts"] = write_attempts
    try:
        return _confirm_warning_page(client, location, base)
    except Exception as exc:  # noqa: BLE001 - Continue may already have been sent
        return _execution_error(base, exc)
