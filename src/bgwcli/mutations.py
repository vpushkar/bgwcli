"""Mutation plans: the browser-equivalent POST payload for a page plus KEY=VALUE overrides.

Ported 1:1 from BGW320-CLI src/mutations.ts. The safety rule here is DANGEROUS_PAGES: a plan for
one of those pages is returned `blocked` unless the caller explicitly asks for a dry run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .errors import UsageError
from .redact import redact_value
from .types import ParsedButton, ParsedPage

DANGEROUS_PAGES = frozenset({"reset", "restart", "routerpasswd", "update"})

# <input> types that are buttons, never data. They reach the payload only through the submit
# button chosen by build_submit_plan.
IGNORED_INPUT_TYPES = frozenset({"button", "submit", "reset", "image"})


@dataclass(frozen=True)
class MutationPlan:
    page: str
    blocked: bool
    raw_payload: dict[str, str]
    display_payload: dict[str, str]
    display_changes: dict[str, str]
    reason: str | None = None
    button: ParsedButton | None = None
    warning: str | None = None


def _resolve_page(page: str) -> str:
    # pages.py owns the tab-name -> page map; fall back to the literal input if it is unavailable.
    try:
        from .pages import resolve_page
    except ImportError:  # pragma: no cover - only during partial builds
        return page
    return resolve_page(page)


def build_mutation_plan(
    page: str,
    parsed: ParsedPage,
    assignments: list[str],
    include_secrets: bool = False,
    *,
    allow_dangerous_dry_run: bool = False,
) -> MutationPlan:
    page = _resolve_page(page)
    changes = parse_assignments(assignments)
    payload = base_payload(parsed)
    payload.update(changes)
    # A browser submits the form through its Save button; without that submit value the gateway
    # answers 302 and silently discards the change (observed live 2026-09-20 on dosprotect).
    save = _find_button(parsed, "save")
    warning = None
    if save is not None:
        payload[save.name] = save.value or save.label or save.name
    else:
        warning = (
            f"page '{page}' has no Save button; the router may accept this POST and discard it. "
            f"Prefer `submit {page} <button> KEY=VALUE...` with the page's real submit button."
        )
    plan = _plan_from_payload(page, payload, changes, include_secrets, allow_dangerous_dry_run)
    return replace(plan, button=save, warning=warning)


def build_submit_plan(
    page: str,
    parsed: ParsedPage,
    button_name: str,
    assignments: list[str],
    include_secrets: bool = False,
) -> MutationPlan:
    page = _resolve_page(page)
    button = _find_button(parsed, button_name)
    if button is None:
        raise UsageError(f"Button '{button_name}' was not found on {page}.")
    changes = parse_assignments(assignments)
    payload = base_payload(parsed)
    payload.update(changes)
    if button.name:
        payload[button.name] = button.value or button.label or button.name
    plan = _plan_from_payload(page, payload, changes, include_secrets, allow_dangerous_dry_run=True)
    return MutationPlan(
        page=plan.page,
        blocked=plan.blocked,
        raw_payload=plan.raw_payload,
        display_payload=plan.display_payload,
        display_changes=plan.display_changes,
        reason=plan.reason,
        button=button,
    )


def _find_button(parsed: ParsedPage, button_name: str) -> ParsedButton | None:
    target = _normalize(button_name)
    for button in parsed.buttons:
        if any(_normalize(candidate) == target for candidate in (button.name, button.value, button.label)):
            return button
    return None


def base_payload(parsed: ParsedPage) -> dict[str, str]:
    """What a browser would post for the page as rendered: enabled data fields, checked
    checkables, enabled selects and textareas. nonce/hashpassword are the client's job."""
    payload: dict[str, str] = {}
    for f in parsed.fields:
        if f.name in ("nonce", "hashpassword"):
            continue
        if f.disabled:
            continue
        if f.type.lower() in IGNORED_INPUT_TYPES:
            continue
        if f.type in ("checkbox", "radio") and not f.checked:
            continue
        payload[f.name] = f.value
    for s in parsed.selects:
        if s.disabled:
            continue
        payload[s.name] = s.value
    for t in parsed.textareas:
        if t.disabled:
            continue
        payload[t.name] = t.value
    return payload


def _plan_from_payload(
    page: str,
    payload: dict[str, str],
    changes: dict[str, str],
    include_secrets: bool,
    allow_dangerous_dry_run: bool,
) -> MutationPlan:
    safe_payload = {name: redact_value(name, value, include_secrets) for name, value in payload.items()}
    safe_changes = {name: redact_value(name, value, include_secrets) for name, value in changes.items()}
    if page in DANGEROUS_PAGES and not allow_dangerous_dry_run:
        return MutationPlan(
            page=page,
            blocked=True,
            reason=f"Refusing to mutate dangerous page '{page}'.",
            raw_payload=payload,
            display_payload=safe_payload,
            display_changes=safe_changes,
        )
    return MutationPlan(
        page=page,
        blocked=False,
        raw_payload=payload,
        display_payload=safe_payload,
        display_changes=safe_changes,
    )


def parse_assignments(assignments: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for assignment in assignments:
        index = assignment.find("=")
        if index <= 0:
            raise UsageError(f"Invalid assignment '{assignment}'. Use KEY=VALUE.")
        parsed[assignment[:index]] = assignment[index + 1 :]
    return parsed


def confirm_token_for_page(page: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", _resolve_page(page).upper())


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())
