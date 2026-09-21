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
from typing import Any, Literal, Protocol

from .mutations import DANGEROUS_PAGES, build_submit_plan
from .redact import redact_value
from .snapshot import (
    FORWARD_COLUMNS,
    IPV4_PATTERN,
    SERVICE_COLUMNS,
    UNCHECKED,
    Snapshot,
    SnapshotForward,
    SnapshotReservation,
    SnapshotService,
    find_column,
)
from .snapshot_diff import FormFieldDiff, SnapshotDiff, pages_missing_from_dump
from .types import ParsedPage

RestoreStepKind = Literal["add-service", "add-forward", "remove-service", "remove-forward", "reserve", "form", "skip"]
RestoreStepStatus = Literal["applied", "skipped", "blocked", "failed", "not-run"]


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


@dataclass(frozen=True)
class RestoreOptions:
    prune: bool = False
    include_secrets: bool = False
    # Restorable page ids selected with `--include`; None plans every section and every form page
    # present in the dump. Sections: services, apphosting (forwards), ipalloc (reservations).
    pages: tuple[str, ...] | None = None


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
            for service in diff.services.missing:
                push(_add_service_step(service, live_pages, options))
            # Service removes are deferred until after the apphosting removes: the gateway silently
            # rejects deleting a custom service that still has a forward (observed live 2026-09-19).
        elif page == "apphosting":
            forwards_selected = selected is None or "apphosting" in selected
            pending_services = {s.service_name or "" for s in steps if s.kind == "add-service" and s.blocked is None}
            if forwards_selected:
                for forward in diff.forwards.missing:
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
                        _remove_step("services", "remove-service", s.name, live_pages, options)
                        for s in diff.services.extra
                    ]
                    for step in _apply_remove_limit(_block_removes_behind_adds(service_removes, steps, "services")):
                        push(step)
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
            # the string alone — a text field whose value happens to be the literal "<unchecked>" is
            # assigned as-is.
            checkable = _checkable_field_names(live_pages.get(page))

            def is_off(c: FormFieldDiff, checkable: set[str] = checkable) -> bool:
                return c.dump == UNCHECKED and c.field in checkable

            assignments = [f"{c.field}={c.dump}" for c in applied if not is_off(c)]
            omit = [c.field for c in applied if is_off(c)]
            shown = ", ".join(
                f"{c.field}: "
                f"{redact_value(c.field, c.live if c.live is not None else '<absent>', options.include_secrets)}"
                f" -> {redact_value(c.field, c.dump if c.dump is not None else '<absent>', options.include_secrets)}"
                for c in applied
            )
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
            if page == "dhcpserver":
                moving = {c.field: c.dump for c in applied if c.field in _LAN_MOVING_FIELDS}
                if moving:
                    new_ip = moving.get("ipaddr")
                    where = f"; reconnect to {new_ip} afterwards" if new_ip else ""
                    step = replace(
                        step,
                        warning=(
                            "this save changes the gateway LAN address/DHCP ("
                            + ", ".join(f"{k}={v}" for k, v in moving.items())
                            + f"); the router will move and the closing diff cannot re-fetch it{where}"
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
    if after_diff.forms:
        return False
    if not prune:
        return True
    return not after_diff.services.extra and not after_diff.forwards.extra


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


def _disabled_field_names(live: ParsedPage | None) -> set[str]:
    names: set[str] = set()
    if live is None:
        return names
    names.update(f.name for f in live.fields if f.disabled)
    names.update(s.name for s in live.selects if s.disabled)
    names.update(t.name for t in live.textareas if t.disabled)
    return names


def _blocked(kind: RestoreStepKind, page: str, description: str, reason: str, **extra: Any) -> RestoreStep:
    return RestoreStep(order=_UNORDERED, kind=kind, page=page, description=description, blocked=reason, **extra)


def _add_service_step(
    service: SnapshotService, live_pages: Mapping[str, ParsedPage], options: RestoreOptions
) -> RestoreStep:
    description = (
        f"add service {service.name} {service.protocol} {service.ext_min_port}-{service.ext_max_port}"
        f" -> {service.int_start_port}"
    )
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
    return replace(step, service_name=service.name)


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
    return _planned(
        "apphosting",
        "add-forward",
        description,
        "Add",
        [f"{service_select.name}={service_option.value}", f"{device_select.name}={device_option.value}"],
        live_pages,
        options,
    )


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
    return _planned(page, kind, description, button, [], live_pages, options)


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


class GetResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def body(self) -> str: ...


class RestorePoster(Protocol):
    """The slice of the router client execute_restore needs (client.BGW320Client satisfies it)."""

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> PostResponse: ...

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


_ERROR_BANNER = re.compile(
    r'<img[^>]*id="error-message-icon"[^>]*icon_error[^>]*>\s*<div[^>]*id="error-message-text"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


def router_error_banner(html: str) -> str | None:
    """The gateway reports a rejected form on the redirect target as a banner, not as an HTTP error:
    `<img id="error-message-icon" src="/images/icon_error.png"> <div id="error-message-text"> A required
    setting is empty ...</div>` (observed live 2026-09-21 on NAT/Gaming adds that answered 302). The same
    text element also carries informational messages such as 'Changes saved' — those come WITHOUT the
    error icon and are not errors. Returns the collapsed error text or None."""
    match = _ERROR_BANNER.search(html)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def error_banner_after_redirect(client: Any, location: str | None) -> str | None:
    """After a 302 to an ordinary CGI page, read that page once and return its error banner text."""
    if not location:
        return None
    match = _CGI_PAGE.search(location)
    if match is None:
        return None
    try:
        page = client.get_cgi_page(match.group(1))
    except Exception:  # noqa: BLE001 - verification only; a failed re-read must not mask the POST result
        return None
    return router_error_banner(page.body)


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
        base = {"order": step.order, "page": step.page, "kind": step.kind, "description": step.description}
        result: RestoreStepResult
        if stopped_at is not None:
            result = RestoreStepResult(**base, status="not-run")
        elif step.kind == "skip":
            result = RestoreStepResult(**base, status="skipped")
        elif step.deferred is not None:
            result = _run_deferred_forward(client, step, base)
            if result.status == "failed":
                stopped_at = step.order
        elif step.blocked is not None or step.raw_payload is None:
            result = RestoreStepResult(**base, status="blocked", error=step.blocked or "no payload")
        else:
            try:
                response = client.post_cgi_page(step.page, step.raw_payload)
                location = _location(response.headers)
                # Limitation: the gateway renders form-level rejections (validation errors, "table
                # full", duplicate entry) as an ordinary HTTP 200 page, and no captured
                # success/error fixture exists to tell those apart from a real save. `applied`
                # therefore means only "the router accepted the POST"; the convergence diff re-run
                # at the end of `restore --commit` is the authoritative success signal.
                if not _accepted(response.status_code):
                    result = RestoreStepResult(
                        **base,
                        status="failed",
                        status_code=response.status_code,
                        location=location,
                        error=f"unexpected HTTP {response.status_code}",
                    )
                    stopped_at = step.order
                elif step.follow_up is not None:
                    result = _run_follow_up(client, step.follow_up, base)
                    if result.status == "failed":
                        stopped_at = step.order
                elif location and _CONFIRMATION_PAGE.search(location):
                    result = _confirm_warning_page(client, location, base)
                    if result.status == "failed":
                        stopped_at = step.order
                else:
                    redirected = 300 <= response.status_code < 400
                    banner = error_banner_after_redirect(client, location) if redirected else None
                    if banner:
                        # A 302 only means the POST was accepted; the router reports a rejected form
                        # as a banner on the redirect target, so surface it as a failure here.
                        result = RestoreStepResult(
                            **base, status="failed", status_code=response.status_code, location=location,
                            error=f"router rejected the change: {banner}",
                        )
                        stopped_at = step.order
                    else:
                        result = RestoreStepResult(
                            **base, status="applied", status_code=response.status_code, location=location
                        )
            except Exception as exc:  # noqa: BLE001 - any transport failure becomes a failed step
                result = RestoreStepResult(**base, status="failed", error=str(exc))
                stopped_at = step.order
        results.append(result)
        if on_step is not None:
            on_step(result)
    return RestoreExecution(steps=results, stopped_at=stopped_at)


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
        resolved = _forward_step(deferred.forward, {"apphosting": parsed}, RestoreOptions())
        if resolved.blocked is not None or resolved.raw_payload is None:
            return RestoreStepResult(
                **base, status="blocked", error=resolved.blocked or "no payload after re-reading apphosting"
            )
        response = client.post_cgi_page("apphosting", resolved.raw_payload)
        location = _location(response.headers)
        if _accepted(response.status_code):
            banner = error_banner_after_redirect(client, location) if 300 <= response.status_code < 400 else None
            if banner:
                return RestoreStepResult(
                    **base, status="failed", status_code=response.status_code, location=location,
                    error=f"router rejected the change: {banner}",
                )
            return RestoreStepResult(**base, status="applied", status_code=response.status_code, location=location)
        return RestoreStepResult(
            **base,
            status="failed",
            status_code=response.status_code,
            location=location,
            error=f"unexpected HTTP {response.status_code}",
        )
    except Exception as exc:  # noqa: BLE001
        return RestoreStepResult(**base, status="failed", error=str(exc))


# Advanced Wi-Fi saves answer 302 -> /cgi-bin/wifiwarn_advanced.ha, a "Wi-Fi Warning" page with
# Continue/Cancel; without Continue the change is discarded (observed live 2026-09-19).
_CONFIRMATION_PAGE = re.compile(r"/cgi-bin/wifiwarn[a-z_]*\.ha", re.IGNORECASE)
_CGI_PAGE = re.compile(r"/cgi-bin/([a-z0-9_]+)\.ha", re.IGNORECASE)


def _confirm_warning_page(client: RestorePoster, location: str, base: dict[str, Any]) -> RestoreStepResult:
    match = _CGI_PAGE.search(location)
    if match is None:
        return RestoreStepResult(
            **base, status="failed", location=location, error=f"cannot derive confirmation page from {location}"
        )
    page = match.group(1)
    warn = client.get_cgi_page(page)
    if warn.status_code != 200:
        return RestoreStepResult(
            **base, status="failed", location=location, error=f"unexpected HTTP {warn.status_code} reading {page}"
        )
    parsed = _parse_page(page, warn.body)
    cont = next((b for b in parsed.buttons if _normalize(b.name) == "continue" and not b.disabled), None)
    if cont is None:
        return RestoreStepResult(
            **base,
            status="failed",
            location=location,
            error=f"{page} has no Continue button; change not confirmed",
        )
    # The Continue button lives in a form whose action is the ORIGINAL page (wconfig.ha), not the
    # warning page; posting it to the warning page is accepted (302) but discards the change.
    owning_form = next((f for f in parsed.forms if cont.name in f.button_names), None)
    target_page = page
    if owning_form is not None:
        action_match = _CGI_PAGE.search(owning_form.action)
        if action_match is not None:
            target_page = action_match.group(1)
    response = client.post_cgi_page(target_page, {cont.name: cont.value or cont.label or cont.name})
    final_location = _location(response.headers)
    if _accepted(response.status_code):
        return RestoreStepResult(**base, status="applied", status_code=response.status_code, location=final_location)
    return RestoreStepResult(
        **base,
        status="failed",
        status_code=response.status_code,
        location=final_location,
        error=f"unexpected HTTP {response.status_code} confirming {target_page}",
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
    if not IPV4_PATTERN.match(follow_up.option_value):
        return RestoreStepResult(
            **base, status="failed", error=f"refusing to post non-IPv4 allocation value '{follow_up.option_value}'"
        )
    parsed = _parse_page(follow_up.page, page.body)
    select = next((s for s in parsed.selects if s.name.lower() == follow_up.select_name.lower()), None)
    mac = re.sub(r"^alloc_", "", follow_up.select_name, flags=re.IGNORECASE)
    if select is None:
        return RestoreStepResult(**base, status="failed", error=f"IP Allocation Entry did not open for {mac}")
    option = next(
        (o for o in select.option_details or [] if o.value == follow_up.option_value and not o.disabled), None
    )
    if option is None:
        return RestoreStepResult(
            **base,
            status="failed",
            error=f"address {follow_up.option_value} not offered (in use by another device?)",
        )
    # The gateway drops POSTs whose submit value does not match the rendered control, so post the
    # value the entry page actually renders (observed "Save" here, "Save..." on wconfig) rather
    # than echoing the control's name.
    save_button = next((b for b in parsed.buttons if b.name == follow_up.button), None)
    if save_button is None:
        return RestoreStepResult(
            **base, status="failed", error=f"IP Allocation Entry has no '{follow_up.button}' button"
        )
    response = client.post_cgi_page(
        follow_up.page,
        follow_up_payload(follow_up, save_button.value or save_button.label or save_button.name),
    )
    location = _location(response.headers)
    if _accepted(response.status_code):
        banner = error_banner_after_redirect(client, location) if 300 <= response.status_code < 400 else None
        if banner:
            return RestoreStepResult(
                **base, status="failed", status_code=response.status_code, location=location,
                error=f"router rejected the change: {banner}",
            )
        return RestoreStepResult(**base, status="applied", status_code=response.status_code, location=location)
    return RestoreStepResult(
        **base,
        status="failed",
        status_code=response.status_code,
        location=location,
        error=f"unexpected HTTP {response.status_code}",
    )


def is_confirmation_redirect(location: str | None) -> bool:
    """True when a POST answered with a redirect to the gateway's Wi-Fi Warning confirmation page."""
    return bool(location) and _CONFIRMATION_PAGE.search(location) is not None


def confirm_warning_page(client: RestorePoster, location: str) -> RestoreStepResult:
    """Follow a Wi-Fi Warning redirect and post Continue to the owning form (shared by set and restore)."""
    base = {"order": 0, "page": "wconfig", "kind": "form", "description": "confirm Wi-Fi Warning"}
    return _confirm_warning_page(client, location, base)
