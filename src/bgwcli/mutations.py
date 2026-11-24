"""Mutation plans: the browser-equivalent POST payload for a page plus KEY=VALUE overrides.

Ported 1:1 from BGW320-CLI src/mutations.ts. The safety rule here is DANGEROUS_PAGES: a plan for
one of those pages is returned `blocked` unless the caller explicitly asks for a dry run.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .actions import is_opener_button
from .errors import UsageError
from .pages import form_target
from .redact import page_sensitive_names, redact_value
from .types import ParsedButton, ParsedForm, ParsedPage

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
    *,
    exact_name: bool = False,
) -> MutationPlan:
    """``exact_name`` (named actions): the button is the one whose HTML name is exactly
    ``button_name``, never one whose value or label happens to read the same."""
    page = _resolve_page(page)
    resolve = _resolve_exact_button if exact_name else _resolve_button
    button = resolve(page, parsed, button_name)
    if button.disabled:
        raise UsageError(f"Button '{button_name}' is disabled on {page}; nothing was posted.")
    require_own_form(page, parsed, button)
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
    # As a browser submits it: only the controls the button's own form owns (its descendants and
    # controls attached with form="<id>"), never a sibling form's fields.
    owner = button._form_index if parsed._form_owners_known else _ANY_FORM
    payload = _without_foreign_allocations(page, base_payload(parsed, owner), changes)
    payload.update(changes)
    if button.name:
        payload[button.name] = button.value or button.label or button.name
    plan = _plan_from_payload(
        page, payload, changes, include_secrets,
        allow_dangerous_dry_run=True, sensitive_names=page_sensitive_names(parsed),
    )
    return replace(plan, button=button)


def _is_own_form(page: str, action: str) -> bool:
    target = form_target(action)
    if not target:
        return True
    path = target.split("?", 1)[0].split("#", 1)[0]
    return path.lower() in (f"{page}.ha", page)


def _resolve_exact_button(page: str, parsed: ParsedPage, button_name: str) -> ParsedButton:
    matches = [b for b in parsed.buttons if b.name == button_name]
    if not matches:
        raise UsageError(f"Button '{button_name}' was not found on {page}.")
    if len(matches) > 1 and len({(b.value, b.disabled) for b in matches}) > 1:
        raise UsageError(f"Button '{button_name}' appears more than once on {page}; nothing was posted.")
    return matches[0]


def _owning_forms(parsed: ParsedPage, button: ParsedButton) -> list[ParsedForm]:
    """The form(s) ``button`` submits: its form owner when the page was parsed from live HTML (none for
    a form-less button), else (a page rebuilt from JSON) every form holding a control of its name."""
    if parsed._form_owners_known:
        index = button._form_index
        return [] if index is None or index >= len(parsed.forms) else [parsed.forms[index]]
    return [form for form in parsed.forms if button.name in form.button_names]


SUBMIT_TYPES = frozenset({"submit", "image"})


def own_submit_button(page: str, parsed: ParsedPage, name: str) -> ParsedButton | None:
    """The enabled submit control named exactly ``name`` that submits ``page``'s own form, as a named
    action posts it; None when there is none. A reset or plain button, a disabled one, or one owned by
    another form is never it. Two such submits rendering different values are ambiguous: UsageError,
    nothing is posted."""
    named = [
        b for b in parsed.buttons if b.name == name and not b.disabled and (b.type or "").lower() in SUBMIT_TYPES
    ]
    own = [b for b in named if _submits_own_form(page, parsed, b)]
    if len({b.value or b.label or b.name for b in own}) > 1:
        raise UsageError(
            f"Button '{name}' appears more than once on {page} with different values; nothing was posted."
        )
    return own[0] if own else None


def _submits_own_form(page: str, parsed: ParsedPage, button: ParsedButton) -> bool:
    owners = _owning_forms(parsed, button)
    if parsed._form_owners_known:
        return bool(owners) and _is_own_form(page, owners[0].action)
    return not owners or any(_is_own_form(page, form.action) for form in owners)


def require_own_form(page: str, parsed: ParsedPage, button: ParsedButton) -> None:
    """Generic submit posts the page's own payload to the page's own CGI. A button that lives in a
    form with a different action (the Wi-Fi restart forms on the home page, or a button attached to
    such a form with form="<id>") is refused before any POST: sending this page's fields to that form
    is a write nobody asked for."""
    if not button.name:
        return
    owners = _owning_forms(parsed, button)
    if not owners or any(_is_own_form(page, form.action) for form in owners):
        return
    from .actions import ROUTER_ACTIONS

    target = form_target(owners[0].action)
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


_ANY_FORM: object = object()


def base_payload(parsed: ParsedPage, owner: int | None | object = _ANY_FORM) -> dict[str, str]:
    """What a browser would post for the page as rendered: enabled data fields, checked
    checkables, enabled selects and textareas. nonce/hashpassword are the client's job.
    ``owner`` (a form index, or None for form-less controls) keeps only the controls that form owns;
    by default every control on the page is read."""
    payload: dict[str, str] = {}
    for f in parsed.fields:
        if f.name in ("nonce", "hashpassword"):
            continue
        if f.disabled or (owner is not _ANY_FORM and f._form_index != owner):
            continue
        if f.type.lower() in IGNORED_INPUT_TYPES:
            continue
        if f.type in ("checkbox", "radio") and not f.checked:
            continue
        payload[f.name] = f.value
    for s in parsed.selects:
        if s.disabled or (owner is not _ANY_FORM and s._form_index != owner):
            continue
        payload[s.name] = s.value
    for t in parsed.textareas:
        if t.disabled or (owner is not _ANY_FORM and t._form_index != owner):
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


def _drop_trailing_newline(text: str) -> str:
    return text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text


def resolve_assignment_values(
    assignments: list[str], *, stdin: Any = None, warn: Callable[[str], None] | None = None
) -> tuple[list[str], bool]:
    """KEY=VALUE assignments with their value sources resolved, so a secret never has to be in argv:
    `KEY=@-` reads the value from stdin (one per invocation), `KEY=@<path>` reads it from that file,
    `KEY=@@text` is the literal `@text`; one trailing newline is dropped from a read value. Returns
    (assignments, whether stdin was read). A file readable by group or others gets a warning (its
    contents are never shown); a file that cannot be read is a UsageError naming only the path."""
    resolved: list[str] = []
    used_stdin = False
    for assignment in assignments:
        index = assignment.find("=")
        if index <= 0:
            resolved.append(assignment)  # parse_assignments reports it
            continue
        key, value = assignment[:index], assignment[index + 1 :]
        if value.startswith("@@"):
            value = value[1:]
        elif value == "@-":
            if used_stdin:
                raise UsageError("Only one KEY=@- may read its value from stdin per command.")
            used_stdin = True
            value = _drop_trailing_newline((stdin if stdin is not None else sys.stdin).read())
        elif value.startswith("@"):
            path = value[1:]
            if not path:
                raise UsageError(f"{key}=@ needs a file path (KEY=@<path>), or KEY=@- for stdin.")
            try:
                with open(path, encoding="utf-8") as handle:
                    mode = os.fstat(handle.fileno()).st_mode
                    value = _drop_trailing_newline(handle.read())
            except (OSError, UnicodeDecodeError) as exc:
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else "not readable UTF-8 text"
                raise UsageError(f"Cannot read the value for {key} from {path}: {reason}.") from None
            if mode & 0o077 and warn is not None:
                warn(f"warning: {path} (value for {key}) is readable by group or others; chmod 600 it.")
            elif mode & 0o077:
                sys.stderr.write(f"warning: {path} (value for {key}) is readable by group or others; chmod 600 it.\n")
        resolved.append(f"{key}={value}")
    return resolved, used_stdin


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
