"""Mutation plans: the browser-equivalent POST payload for a page plus KEY=VALUE overrides.

Ported 1:1 from BGW320-CLI src/mutations.ts. The safety rule here is DANGEROUS_PAGES: a plan for
one of those pages is returned `blocked` unless the caller explicitly asks for a dry run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from .actions import is_opener_button
from .errors import UsageError
from .redact import page_sensitive_names, redact_value
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
    # Names displayed redacted (by name or by password control type); the post-save verification
    # redacts its mismatches with the same set.
    sensitive_names: frozenset[str] = field(default=frozenset(), compare=False, metadata={"serialize": False})


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
    payload = _without_foreign_allocations(page, base_payload(parsed), changes)
    payload.update(changes)
    # A browser submits the form through its Save button; without that submit value the gateway
    # answers 302 and silently discards the change (observed live 2026-09-20 on dosprotect).
    save = _find_button(parsed, "save")
    if save is not None and save.disabled:
        # Same refusal as `submit`: a browser cannot activate a disabled control.
        raise UsageError(f"Button 'Save' is disabled on {page}; nothing was posted.")
    warning = None
    if save is not None:
        payload[save.name] = save.value or save.label or save.name
    else:
        warning = (
            f"page '{page}' has no Save button; the router may accept this POST and discard it. "
            f"Prefer `submit {page} <button> KEY=VALUE...` with the page's real submit button."
        )
    plan = _plan_from_payload(
        page, payload, changes, include_secrets, allow_dangerous_dry_run, page_sensitive_names(parsed)
    )
    return replace(plan, button=save, warning=warning)


def build_submit_plan(
    page: str,
    parsed: ParsedPage,
    button_name: str,
    assignments: list[str],
    include_secrets: bool = False,
) -> MutationPlan:
    page = _resolve_page(page)
    button = _resolve_button(page, parsed, button_name)
    if button.disabled:
        raise UsageError(f"Button '{button_name}' is disabled on {page}; nothing was posted.")
    _require_own_form(page, parsed, button)
    if assignments and is_opener_button(page, button.name or ""):
        # The opener and an assignment in one POST has an unknown effect on the gateway, and a dry run
        # showing such a payload would be misleading: the documented flow is two steps.
        token = confirm_token_for_page(page)
        raise UsageError(
            f"Button '{button_name}' only opens an editor on {page}; it takes no KEY=VALUE assignments. "
            f"Open the editor with `submit {page} {button_name} --commit --confirm {token}`, then "
            f"`set {page} KEY=VALUE --commit --confirm {token}`. Nothing was posted."
        )
    changes = parse_assignments(assignments)
    payload = _without_foreign_allocations(page, base_payload(parsed), changes)
    payload.update(changes)
    if button.name:
        payload[button.name] = button.value or button.label or button.name
    plan = _plan_from_payload(
        page, payload, changes, include_secrets,
        allow_dangerous_dry_run=True, sensitive_names=page_sensitive_names(parsed),
    )
    return replace(plan, button=button)


def _form_target(action: str) -> str:
    """A form action reduced to what it posts to: the part after `/cgi-bin/`, query kept."""
    return action.strip().rpartition("/cgi-bin/")[2] if "/cgi-bin/" in action else action.strip()


def _is_own_form(page: str, action: str) -> bool:
    target = _form_target(action)
    if not target:
        return True
    path = target.split("?", 1)[0].split("#", 1)[0]
    return path.lower() in (f"{page}.ha", page)


def _require_own_form(page: str, parsed: ParsedPage, button: ParsedButton) -> None:
    """Generic submit posts the page's own payload to the page's own CGI. A button that lives in a
    form with a different action (the Wi-Fi restart forms on the home page) is refused before any
    POST: sending this page's fields to that form is a write nobody asked for."""
    if not button.name:
        return
    owners = [form for form in parsed.forms if button.name in form.button_names]
    if not owners or any(_is_own_form(page, form.action) for form in owners):
        return
    from .actions import ROUTER_ACTIONS

    target = _form_target(owners[0].action)
    for action in ROUTER_ACTIONS:
        if action.page == page and action.post_path == target:
            raise UsageError(
                f"Button '{button.name}' on {page} belongs to the form that posts to {target}, not to "
                f"{page}'s own form; use 'action {action.name}'. Nothing was posted."
            )
    raise UsageError(
        f"Button '{button.name}' on {page} belongs to another form (it posts to {target}); generic "
        "submit does not post to it. Nothing was posted."
    )


def _without_foreign_allocations(page: str, payload: dict[str, str], changes: dict[str, str]) -> dict[str, str]:
    """The gateway keeps an "IP Allocation Entry" block rendered for the rest of the web session after
    any Allocate/Cancel, so the IP Allocation page's base payload can carry `alloc_<other mac>=normal`
    ("Address from DHCP pool"). Posting it releases that other device's reservation, so on ipalloc only
    the allocation selects the user assigns are ever sent (restore's reserve step does the same)."""
    if page != "ipalloc":
        return payload
    return {k: v for k, v in payload.items() if not k.lower().startswith("alloc_") or k in changes}


def _matching_buttons(parsed: ParsedPage, button_name: str) -> list[ParsedButton]:
    target = _normalize(button_name)
    return [
        button for button in parsed.buttons
        if any(_normalize(candidate) == target for candidate in (button.name, button.value, button.label))
    ]


def _resolve_button(page: str, parsed: ParsedPage, button_name: str) -> ParsedButton:
    """The one button `submit` means: the only match, or - when the text also matches other buttons'
    value or label - the single button whose `name` is exactly the text (case-sensitive)."""
    matches = _matching_buttons(parsed, button_name)
    if not matches:
        raise UsageError(f"Button '{button_name}' was not found on {page}.")
    if len(matches) == 1:
        return matches[0]
    exact = [b for b in matches if b.name == button_name]
    if len(exact) == 1:
        return exact[0]
    names = ", ".join(sorted({b.name or b.value or b.label for b in matches}))
    raise UsageError(
        f"Button '{button_name}' matches more than one button on {page} ({names}); "
        "name the button exactly. Nothing was posted."
    )


def _find_button(parsed: ParsedPage, button_name: str) -> ParsedButton | None:
    matches = _matching_buttons(parsed, button_name)
    return matches[0] if matches else None


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
    sensitive_names: frozenset[str] = frozenset(),
) -> MutationPlan:
    def safe(values: dict[str, str]) -> dict[str, str]:
        return {name: redact_value(name, value, include_secrets, sensitive_names) for name, value in values.items()}

    safe_payload = safe(payload)
    safe_changes = safe(changes)
    if page in DANGEROUS_PAGES and not allow_dangerous_dry_run:
        return MutationPlan(
            page=page,
            blocked=True,
            reason=f"Refusing to mutate dangerous page '{page}'.",
            raw_payload=payload,
            display_payload=safe_payload,
            display_changes=safe_changes,
            sensitive_names=sensitive_names,
        )
    return MutationPlan(
        page=page,
        blocked=False,
        raw_payload=payload,
        display_payload=safe_payload,
        display_changes=safe_changes,
        sensitive_names=sensitive_names,
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
