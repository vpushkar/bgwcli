"""Operation records printed (text or --json) after action / set / submit / diagnostics / restore.

Port of src/operations.ts. `OperationResult` serializes with camelCase keys via types.to_json_dict;
None-valued fields are omitted, matching the TS objects that leave those keys undefined.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from .actions import RouterAction
from .mutations import DANGEROUS_PAGES

OperationKind = Literal["action", "set", "submit", "diagnostic", "restore"]


@dataclass(frozen=True)
class OperationResult:
    operation: OperationKind
    dry_run: bool
    committed: bool
    page: str
    guarded: bool
    dangerous: bool
    confirmation: str | None = None
    commit_command: str | None = None
    action: str | None = None
    button: str | None = None
    target: str | None = None
    changes: dict[str, str] | None = None
    payload: dict[str, str] | None = None
    status_code: int | None = None
    location: str | None = None
    result: str | None = None
    verified: bool | None = None
    mismatches: dict[str, dict[str, str]] | None = None
    warning: str | None = None
    outcome: str | None = None
    reconnect_address: str | None = None
    write_attempted: bool | None = None
    # True when the gateway answered the write POST (any status), False when no answer arrived.
    write_response_received: bool | None = None
    write_performed: bool | None = None
    acknowledgement_observed: bool | None = None
    # Configuration POSTs sent for this write. Set to 2 on a write the transport re-sent after a Login-page
    # answer (whether or not the re-send succeeded), and to the transport's count on a failed POST (which
    # can be 1); absent on a successful single POST and when the count was not observed.
    write_attempts: int | None = None
    # False when the action's redirect answer was deliberately not read (the effect drops the
    # gateway's web server); absent when the answer was read.
    answer_read: bool | None = None
    # Number of post-save verification re-reads performed (1 = first try); absent when none ran.
    verify_attempts: int | None = None


_SAFE_ARG = re.compile(r"^[a-z0-9_.:-]+$", re.IGNORECASE)


def quote_arg(value: str) -> str:
    """Single-quote anything that is not plain [a-z0-9_.:-] so generated commands are shell-safe."""
    if _SAFE_ARG.match(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def action_dry_run(action: RouterAction, display_payload: dict[str, str]) -> OperationResult:
    return OperationResult(
        operation="action",
        dry_run=True,
        committed=False,
        page=action.page,
        action=action.name,
        guarded=True,
        dangerous=action.dangerous,
        confirmation=action.confirm_token,
        commit_command=f"action {action.name} --commit --confirm {action.confirm_token}",
        payload=display_payload,
    )


def action_committed(action: RouterAction, status_code: int | None, location: str | None) -> OperationResult:
    return OperationResult(
        operation="action",
        dry_run=False,
        committed=True,
        page=action.page,
        action=action.name,
        guarded=True,
        dangerous=action.dangerous,
        confirmation=action.confirm_token,
        status_code=status_code,
        location=location,
    )


def set_dry_run(plan: Any, confirmation: str | None) -> OperationResult:
    """`plan` is a mutations.MutationPlan (.page, .display_changes, .display_payload)."""
    if confirmation:
        commit_command = f"set {quote_arg(plan.page)} KEY=VALUE... --commit --confirm {quote_arg(confirmation)}"
    else:
        commit_command = f"set {quote_arg(plan.page)} KEY=VALUE... --commit"
    return OperationResult(
        operation="set",
        dry_run=True,
        committed=False,
        page=plan.page,
        guarded=confirmation is not None,
        dangerous=plan.page in DANGEROUS_PAGES,
        commit_command=commit_command,
        changes=plan.display_changes,
        payload=plan.display_payload,
        confirmation=confirmation or None,
        warning=getattr(plan, "warning", None),
    )


def set_committed(
    page: str,
    status_code: int,
    location: str | None,
    *,
    verified: bool | None = None,
    mismatches: dict[str, dict[str, str]] | None = None,
    warning: str | None = None,
) -> OperationResult:
    """`verified` is the post-commit re-read outcome: True when every requested field reads back as
    requested, False with `mismatches` otherwise, None when the page could not be re-read."""
    return OperationResult(
        operation="set",
        dry_run=False,
        committed=True,
        page=page,
        guarded=True,
        dangerous=page in DANGEROUS_PAGES,
        status_code=status_code,
        location=location,
        verified=verified,
        mismatches=mismatches,
        warning=warning,
    )


def submit_dry_run(plan: Any, requested_button: str, confirmation: str) -> OperationResult:
    """`plan` is a mutations.MutationPlan; `plan.button` (ParsedButton | None) supplies the display label."""
    resolved = getattr(plan, "button", None)
    button = resolved.label if resolved is not None else requested_button
    return OperationResult(
        operation="submit",
        dry_run=True,
        committed=False,
        page=plan.page,
        button=button,
        guarded=True,
        dangerous=plan.page in DANGEROUS_PAGES,
        confirmation=confirmation,
        commit_command=f"submit {plan.page} {quote_arg(requested_button)} --commit --confirm {confirmation}",
        changes=plan.display_changes,
        payload=plan.display_payload,
    )


def submit_committed(
    page: str,
    button: str,
    status_code: int,
    location: str | None,
    *,
    verified: bool | None = None,
    mismatches: dict[str, dict[str, str]] | None = None,
    warning: str | None = None,
) -> OperationResult:
    """`verified`/`mismatches`/`warning` carry the live re-read, run when an acknowledged save did
    not show its requested state."""
    return OperationResult(
        operation="submit",
        dry_run=False,
        committed=True,
        page=page,
        button=button,
        guarded=True,
        dangerous=page in DANGEROUS_PAGES,
        status_code=status_code,
        location=location,
        verified=verified,
        mismatches=mismatches,
        warning=warning,
    )


def diagnostic_dry_run(kind: str, target: str, payload: dict[str, str], button: str) -> OperationResult:
    return OperationResult(
        operation="diagnostic",
        dry_run=True,
        committed=False,
        page="diag",
        action=kind,
        target=target,
        button=button,
        guarded=True,
        dangerous=False,
        confirmation="DIAG",
        commit_command=f"diagnostics {kind} {quote_arg(target)} --commit --confirm DIAG",
        payload=payload,
    )


def diagnostic_committed(kind: str, target: str, status_code: int | None, result: str | None) -> OperationResult:
    return OperationResult(
        operation="diagnostic",
        dry_run=False,
        committed=True,
        page="diag",
        action=kind,
        target=target,
        guarded=True,
        dangerous=False,
        status_code=status_code,
        result=result,
    )


def restore_dry_run(steps: Iterable[Any], confirmation: str) -> OperationResult:
    """`steps` are restore.RestoreStep records (.kind, .blocked)."""
    steps = list(steps)
    blocked = sum(1 for step in steps if getattr(step, "blocked", None))
    skipped = sum(1 for step in steps if step.kind == "skip")
    return OperationResult(
        operation="restore",
        dry_run=True,
        committed=False,
        page="restore",
        guarded=True,
        dangerous=False,
        confirmation=confirmation,
        commit_command=f"bgwcli restore <dumpfile> --commit --confirm {confirmation}",
        result=f"{len(steps)} steps planned, {blocked} blocked, {skipped} skipped",
    )


def restore_committed(execution: Any) -> OperationResult:
    """`execution` is a restore.RestoreExecution (.steps with .status/.status_code, .stopped_at)."""
    steps = list(execution.steps)

    def count(status: str) -> int:
        return sum(1 for step in steps if step.status == status)

    last_applied = next((step for step in reversed(steps) if step.status == "applied"), None)
    summary = f"{count('applied')} applied, {count('failed')} failed, {count('not-run')} not run"
    if count("blocked"):
        summary += f", {count('blocked')} blocked"
    if count("skipped"):
        summary += f", {count('skipped')} skipped"
    if count("reconnect-required"):
        summary += f", {count('reconnect-required')} reconnect required"
    if count("unchanged"):
        summary += f", {count('unchanged')} unchanged"
    def confirmed_write(step: Any) -> bool:
        performed = getattr(step, "write_performed", None)
        return performed is True or (performed is None and step.status == "applied")

    def no_write(step: Any) -> bool:
        performed = getattr(step, "write_performed", None)
        if performed is not None:
            return performed is False
        if step.status == "applied":
            return False
        return getattr(step, "write_attempted", None) is False or step.status in {
            "blocked", "skipped", "not-run", "unchanged",
        }

    committed = any(confirmed_write(step) for step in steps)
    no_save = all(no_write(step) for step in steps)
    if count("failed"):
        outcome = "failed"
    elif count("blocked") or count("not-run") or count("reconnect-required"):
        outcome = "incomplete"
    else:
        outcome = "unchanged" if no_save else None
    stopped_at = getattr(execution, "stopped_at", None)
    return OperationResult(
        operation="restore",
        dry_run=False,
        committed=committed,
        write_performed=True if committed else (False if no_save else None),
        outcome=outcome,
        page="restore",
        guarded=True,
        dangerous=False,
        result=summary if stopped_at is None else f"{summary}; stopped at step {stopped_at}",
        status_code=getattr(last_applied, "status_code", None) if last_applied is not None else None,
    )
