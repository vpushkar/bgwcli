"""Unattended factory-reset recovery: detect that the gateway lost the dump's configuration and put
it back, without ever acting on ordinary drift.

Designed for a systemd timer (see deploy/): every run fetches the same pages `diff`/`restore` use,
diffs them against the baseline dump and asks `detect_factory_reset` whether the difference looks
like a reset (EVERY dumped service, forward and reservation gone) rather than a deliberate manual
change (some entries gone, values edited). A reset or matching unfinished recovery triggers passes, which never
prune: router-only entries are always left alone. The access-code fallback (the printed sticker code
comes back after a reset) lives in cli.py; this module records that it was used and names it in the
reason when the diff is reset-shaped or a recovery is unfinished; on its own it is no reset signal
(the result carries a warning to re-set the access code instead).

Exit-code contract (AutorestoreResult.exit_code): 0 when there was nothing to do or the restore
converged or the router is unreachable (timers must stay quiet), 1 when a restore is needed
(dry-run) or did not converge, 2 when the command could not answer after writing to the router
(including a configuration write that got no answer: transport failure, HTTP error, session-wide
authentication fault or full session pool, even when the closing diff is readable; the result
then carries write_unanswered=True), or when the same failure ended RETRY_LIMIT consecutive runs
of one recovery (the intent record keeps a hash of the last failure and its count); such a
stopped recovery sends nothing until the intent file is removed or the gateway is found converged.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from . import format as fmt
from .allocation_preflight import (
    AllocationRescanEvidence,
    OwnershipSnapshotPages,
    allocation_preflight_report,
    inspect_allocation_conflicts,
    rescan_allocation_conflicts,
)
from .client import NoncePageRefusedError, RouterResponseError, pool_full_metadata
from .errors import BgwError, RouterAuthError, RouterConnectionError, RouterSessionPoolFullError
from .fetch import ParsedPageResult
from .recovery_state import Checkpoint
from .restore import (
    CONTINUE_NOT_POSTED,
    RestoreExecution,
    RestoreOptions,
    RestoreStep,
    RestoreStepResult,
    build_restore_plan,
    execute_restore,
    pending_allocation_requests,
    reconnect_verification_reason,
    restore_converged,
)
from .snapshot import (
    OPTIONAL_FORM_PAGES,
    Snapshot,
    extract_snapshot,
    forward_key,
    reservation_key,
    service_key,
    snapshot_read_pages,
)
from .snapshot_diff import SnapshotDiff, diff_snapshots, holds_convergence_open
from .types import ParsedPage, to_json_dict
from .write_outcome import unanswered_write_step

AutorestoreStatus = Literal["no-reset", "restore-needed", "converged", "not-converged", "router-unreachable", "error"]

EXIT_CODES: Mapping[str, int] = {
    "no-reset": 0,
    "restore-needed": 1,
    "converged": 0,
    "not-converged": 1,
    "router-unreachable": 0,
    "error": 2,
}
FALLBACK_CODE_REASON = "access code reverted; factory reset suspected"
FALLBACK_CODE_WARNING = (
    "logged in with BGW_FALLBACK_ACCESS_CODE but the gateway still holds the dump's configuration "
    "(no reset-shaped difference): left alone; set BGW_ACCESS_CODE to the gateway's current access code, "
    "or set the gateway's access code back"
)
# Consecutive runs of one recovery intent ending with the same failure before autorestore stops
# writing on its own and exits 2: a timer must not re-post the same doomed writes forever.
RETRY_LIMIT = 3
_sleep = time.sleep  # seam for tests (cli passes nothing; run_autorestore(sleep=...) overrides)
_monotonic = time.monotonic  # seam for the run's time limit

_SECTIONS: tuple[tuple[str, str, str], ...] = (
    # (label, dump attribute, diff attribute)
    ("services", "services", "services"),
    ("forwards", "forwards", "forwards"),
    ("reservations", "reservations", "reservations"),
)
_SECTION_PAGE = {"services": "services", "forwards": "apphosting", "reservations": "ipalloc"}

# fetch_pages(client, page_ids) -> (parsed pages, failures); cli passes _fetch_snapshot_pages.
FetchPages = Callable[[Any, Sequence[str]], tuple[dict[str, ParsedPage], list[ParsedPageResult]]]
# client_factory() -> (authenticated client, used_fallback_code). cli wraps login + fallback here.
ClientFactory = Callable[[], tuple[Any, bool]]
# relogin(error) -> a fresh (client, used_fallback_code) after the first page read was refused for an
# auth reason, or None to let the error stand. cli uses it when an imported cached session turns out
# dead: login() was a no-op for it, so the rejection only shows on the first read.
Relogin = Callable[[RouterAuthError], "tuple[Any, bool] | None"]


@dataclass(frozen=True)
class ResetSignature:
    """Per-section counts behind a detection decision: how much of the dump the router still lacks."""

    missing: dict[str, int]
    totals: dict[str, int]

    @classmethod
    def from_diff(cls, diff: SnapshotDiff, dump: Snapshot, *, pages: Sequence[str] | None = None) -> ResetSignature:
        missing: dict[str, int] = {}
        totals: dict[str, int] = {}
        for label, dump_attr, diff_attr in _SECTIONS:
            selected = pages is None or _SECTION_PAGE[label] in pages
            totals[label] = len(getattr(dump, dump_attr)) if selected else 0
            missing[label] = len(getattr(diff, diff_attr).missing) if selected else 0
        dumped_forms = [page for page in dump.forms if pages is None or page in pages]
        totals["forms"] = len(dumped_forms)
        # Only a difference restore can act on votes: a live-only control (newer firmware) or one
        # the live page renders disabled cannot be restored (restore_converged ignores them).
        missing["forms"] = sum(
            1 for page in dumped_forms if any(holds_convergence_open(change) for change in diff.forms.get(page, ()))
        )
        return cls(missing=missing, totals=totals)

    def voting_sections(self) -> list[str]:
        """Sections the dump actually has entries for; only those can vote for a reset."""
        return [label for label, _, _ in _SECTIONS if self.totals[label] > 0]


def detect_factory_reset(
    diff: SnapshotDiff,
    dump: Snapshot,
    *,
    on_any_diff: bool = False,
    pages: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """(detected, reason). A reset means EVERY dumped entry of EVERY non-empty section is missing;
    a dump with no sections falls back to every dumped form page differing. Partial loss is drift."""
    if diff.identical:
        return False, "no differences"
    # Router-only entries (autorestore never prunes), live-only controls and controls the live page
    # renders disabled are differences no restore pass can change; a recovery started for them
    # would post nothing and repeat on every run, even with --on-any-diff.
    if restore_converged(diff, False):
        return False, "nothing restore can act on differs"
    if on_any_diff:
        return True, "differences found (on-any-diff)"
    signature = ResetSignature.from_diff(diff, dump, pages=pages)
    voting = signature.voting_sections()
    if voting:
        reason = "; ".join(f"{label} {signature.missing[label]}/{signature.totals[label]} missing" for label in voting)
        return all(signature.missing[label] == signature.totals[label] for label in voting), reason
    if signature.totals["forms"] > 0:
        reason = f"{signature.missing['forms']}/{signature.totals['forms']} form pages differ"
        return signature.missing["forms"] == signature.totals["forms"], reason
    return False, "dump has nothing to compare (no sections, no form pages)"


# The run's own time limit: no further step, pass or inter-pass sleep starts once this many seconds
# have passed since the run began. It is what the service unit's start timeout is sized to (see
# deploy/bgw-autorestore.service): the unit allows this limit, plus the one step that may be in flight
# (nonce read 15 s + POST 15 s + acknowledgement window 60 s), plus 555 s as an upper bound for the
# closing fetch (the 11 snapshot pages, 7 always plus up to 4 optional form pages, at the 15 s page
# timeout; the figure is kept generous and keeps the 2580 s arithmetic unchanged), plus a margin.
RUN_DEADLINE_SECONDS = 1800.0
IN_FLIGHT_STEP_SECONDS = 90.0
CLOSING_FETCH_SECONDS = 555.0
UNIT_MARGIN_SECONDS = 135.0
UNIT_START_TIMEOUT_SECONDS = (
    RUN_DEADLINE_SECONDS + IN_FLIGHT_STEP_SECONDS + CLOSING_FETCH_SECONDS + UNIT_MARGIN_SECONDS
)


class _RunDeadlineReached(Exception):
    """Raised from the step callback to stop a pass between two steps once the run's time is up."""


@dataclass(frozen=True)
class AutorestoreOptions:
    commit: bool
    max_passes: int = 3
    wait_seconds: int = 120
    max_run_seconds: float = RUN_DEADLINE_SECONDS
    on_any_diff: bool = False
    # Restorable page ids (see snapshot_diff.RESTORABLE_PAGES); None = everything present in the dump.
    pages: tuple[str, ...] | None = None


@dataclass
class AutorestoreResult:
    status: AutorestoreStatus
    reason: str
    detected: bool = False
    used_fallback_code: bool = False
    passes: list[dict[str, Any]] = field(default_factory=list)
    # Detection-time counts (what the router lacked when the run started); final_diff is the end state.
    missing: dict[str, int] = field(default_factory=dict)
    # Raw objects for the text printers; result_output() renders their redacted --json views.
    final_diff: SnapshotDiff | None = None
    plan: list[RestoreStep] | None = None
    allocation_preflight: dict[str, Any] | None = None
    session_pool_full: bool = False
    waited_ms: int | None = None
    retry_count: int | None = None
    warnings: list[str] = field(default_factory=list)
    # A configuration write in this run went out (or may have) and got no answer; status is "error".
    write_unanswered: bool = False

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]


def result_output(result: AutorestoreResult) -> dict[str, Any]:
    """--json view: camelCase fields, exitCode, the final diff redacted like `diff --json`, the plan
    like `restore --json` (rawPayload dropped, assignments redacted)."""
    out = to_json_dict(replace(result, final_diff=None, plan=None))
    out["exitCode"] = result.exit_code
    out["diff"] = fmt.display_diff(result.final_diff, False) if result.final_diff is not None else None
    out["plan"] = fmt.display_restore_steps(result.plan, False) if result.plan is not None else None
    if result.allocation_preflight is not None and result.plan is None:
        out["planStatus"] = result.allocation_preflight["planStatus"]
        out["planComplete"] = False
    return out


def _pass_summary(number: int, execution: Any, converged: bool | None) -> dict[str, Any]:
    counts = {status: 0 for status in ("applied", "unchanged", "blocked", "failed", "skipped", "not-run")}
    for step in execution.steps:
        counts[step.status] = counts.get(step.status, 0) + 1
    return {
        "pass": number,
        "applied": counts["applied"],
        "unchanged": counts["unchanged"],
        "blocked": counts["blocked"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "notRun": counts["not-run"],
        "stoppedAt": execution.stopped_at,
        "converged": converged,
        "steps": to_json_dict(execution)["steps"],
    }


@dataclass
class _ExecutionFacts:
    """One pass's write evidence and stop conditions, selected in execution order."""

    # A configuration POST went out (or may have), whatever the gateway answered: the recovery
    # intent and its failure count must survive even when the answer was "No changes detected".
    mutation_possible: bool = False
    failed_step: RestoreStepResult | None = None
    reconnect_step: RestoreStepResult | None = None
    pool_step: RestoreStepResult | None = None

    @classmethod
    def from_steps(cls, steps: Sequence[RestoreStepResult]) -> _ExecutionFacts:
        facts = cls()
        for step in steps:
            facts.mutation_possible |= step.write_attempted is not False
            if facts.failed_step is None and step.status in {"failed", "reconnect-required"}:
                facts.failed_step = step
            if facts.reconnect_step is None and (step.status == "reconnect-required" or step.lan_address_changed):
                facts.reconnect_step = step
            if facts.pool_step is None and step.session_pool_full:
                facts.pool_step = step
        return facts


def _step_identity(step: RestoreStep | RestoreStepResult) -> tuple[str, ...]:
    """What one step writes, stable across re-planned passes: a form page is one Save whatever its
    differing fields; an entry step is its page, kind and entry (the reserve description's live
    "(currently ...)" suffix changes between passes, so it is dropped)."""
    if step.kind == "form":
        return (step.page, step.kind)
    description = step.description.split(" (currently ", 1)[0] if step.kind == "reserve" else step.description
    return (step.page, step.kind, description)


def _acknowledged(step: RestoreStepResult) -> bool:
    """A configuration POST went out and the gateway answered it (saved, or "No changes detected")."""
    return step.status in {"applied", "unchanged"} and step.write_attempted is not False


def _without_reposts(steps: Sequence[RestoreStep], acknowledged: Mapping[tuple[str, ...], int]) -> list[RestoreStep]:
    """A later pass never re-posts a write the gateway acknowledged earlier in this invocation: such
    a step becomes a skip; blocked, deferred and never-posted steps are retried as planned."""
    kept: list[RestoreStep] = []
    for step in steps:
        number = acknowledged.get(_step_identity(step)) if step.kind != "skip" else None
        if number is None:
            kept.append(step)
            continue
        kept.append(replace(
            step, kind="skip", raw_payload=None, blocked=None, deferred=None, follow_up=None,
            description=f"{step.description}: acknowledged in pass {number}; not re-posted in this run",
        ))
    return kept


def _pre_send_fault_label(step: RestoreStepResult) -> str:
    """What stopped a step before its POST, named for the reason text."""
    if step.session_pool_full:
        return f"the session pool was full before the {step.page} write was sent"
    if step.error_type == "RouterAuthError" and step.error_page_level:
        status = f" (HTTP {step.error_status_code})" if step.error_status_code else ""
        return f"the gateway refused a page read{status} before the {step.page} write was sent"
    if step.error_type == "RouterAuthError":
        return f"the gateway session was lost (login page or failed login) before the {step.page} write was sent"
    if step.error_type == "NoncePageRefusedError":
        return f"the page read for the {step.page} write nonce was refused before the write was sent"
    if step.error_type == "SnapshotExtractionError":
        return f"the {step.page} page could not be read before the write was sent"
    return f"{step.error_type} before the {step.page} write was sent"


def _open_differences(diff: SnapshotDiff | None) -> dict[str, Any] | None:
    """The restorable differences still open, by identity only (no values): the failure shape."""
    if diff is None:
        return None
    return {
        "services": sorted(service_key(entry) for entry in diff.services.missing),
        "forwards": sorted(forward_key(entry) for entry in diff.forwards.missing),
        "reservations": sorted(
            [reservation_key(entry) for entry in diff.reservations.missing]
            + [change.mac.lower() for change in diff.reservations.changed]
        ),
        "forms": {
            page: sorted(change.field for change in changes if holds_convergence_open(change))
            for page, changes in sorted(diff.forms.items())
            if any(holds_convergence_open(change) for change in changes)
        },
    }


def _failure_fingerprint(shape: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()


def _failure_reason(failures: Sequence[ParsedPageResult]) -> str:
    return "; ".join(f"{f.page}: {f.error or 'page unavailable'}" for f in failures)


def run_autorestore(
    client_factory: ClientFactory,
    dump: Snapshot,
    options: AutorestoreOptions,
    *,
    fetch_pages: FetchPages,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] = print,
    on_step: Callable[[RestoreStepResult], None] | None = None,
    ts: str = "",
    router_host: str = "",
    checkpoint: Checkpoint | None = None,
    relogin: Relogin | None = None,
    monotonic: Callable[[], float] | None = None,
) -> AutorestoreResult:
    """Login -> fetch -> diff -> detect; then (commit only) restore passes until converged.

    Anything that goes wrong before the first POST is reported as "router-unreachable" (exit 0, one
    log line): the gateway's web UI hangs routinely and a timer must not page on that. After a POST
    the router has been written to, so a failed re-read is an "error" (exit 2). A failed write stops
    all passes after the final read-only diff; a later invocation must refetch and replan before retrying."""
    pause = sleep if sleep is not None else _sleep
    clock = monotonic if monotonic is not None else _monotonic
    started = clock()

    def out_of_time(extra: float = 0.0) -> bool:
        return clock() - started + extra >= options.max_run_seconds

    optional = tuple(page for page in OPTIONAL_FORM_PAGES if page in dump.forms)
    page_ids = snapshot_read_pages(dump, options.pages)
    restore_options = RestoreOptions(prune=False, include_secrets=False, pages=options.pages)

    def snapshot(parsed: Mapping[str, ParsedPage]) -> SnapshotDiff:
        live = extract_snapshot(parsed, ts=ts, router_host=router_host, include=optional, selected_pages=options.pages)
        return diff_snapshots(dump, live, pages=options.pages)

    try:
        client, used_fallback = client_factory()
        try:
            parsed, failures = fetch_pages(client, page_ids)
        except RouterAuthError as error:
            fresh = relogin(error) if relogin is not None else None
            if fresh is None:
                raise
            client, used_fallback = fresh
            parsed, failures = fetch_pages(client, page_ids)
    except (RouterConnectionError, RouterResponseError) as error:
        log(f"router-unreachable: {error}")
        return AutorestoreResult(status="router-unreachable", reason=str(error))
    if failures:
        reason = _failure_reason(failures)
        if any(failure.structural or failure.status_code in (401, 403) for failure in failures):
            # The gateway answered with something that is not the page (Login page, "Page not found",
            # a table page without its table) or refused this client (a page-level 401/403): no
            # baseline comparison is possible, and that is a finding about the gateway or the access
            # code, not a lost connection. Transport faults and 5xx answers stay quiet below.
            log(f"error: {reason}")
            return AutorestoreResult(status="error", reason=reason)
        log(f"router-unreachable: {reason}")
        return AutorestoreResult(status="router-unreachable", reason=reason)

    diff = snapshot(parsed)
    detected, reason = detect_factory_reset(diff, dump, on_any_diff=options.on_any_diff, pages=options.pages)
    try:
        recovering = checkpoint is not None and checkpoint.is_active()
    except OSError as exc:
        # An intent record that exists but cannot be read is unknown recovery state: no answer.
        reason = f"recovery checkpoint read failed: {exc}"
        log(f"error: {reason}")
        return AutorestoreResult(
            status="error", reason=reason, detected=detected, used_fallback_code=used_fallback, final_diff=diff,
        )
    warnings: list[str] = []
    if used_fallback:
        # The fallback code only corroborates a reset-shaped diff or an unfinished recovery: the
        # sticker code also works after someone set it back by hand, and ordinary drift must stay.
        if detected or recovering:
            log(FALLBACK_CODE_REASON)
            reason = f"{FALLBACK_CODE_REASON}; {reason}"
        else:
            warnings.append(FALLBACK_CODE_WARNING)
            log(f"warning: {FALLBACK_CODE_WARNING}")
    if recovering:
        detected, reason = True, "resuming unfinished recovery; " + reason
    signature = ResetSignature.from_diff(diff, dump, pages=options.pages)
    result = AutorestoreResult(
        status="no-reset",
        reason=reason,
        detected=detected,
        used_fallback_code=used_fallback,
        missing=signature.missing,
        final_diff=diff,
        warnings=warnings,
    )
    if not detected:
        log(f"no-reset: {reason}")
        return result

    def checkpoint_action(action: str, *, secondary: str | None = None) -> bool:
        if checkpoint is None:
            return True
        try:
            getattr(checkpoint, action)()
        except OSError as exc:
            result.status = "error"
            detail = f"{secondary or f'recovery checkpoint {action}'} failed: {exc}"
            result.reason = f"{result.reason}; {detail}" if secondary else detail
            log(f"error: {result.reason}")
            return False
        return True

    if recovering and restore_converged(diff, False):
        if options.commit:
            if not checkpoint_action("finish"):
                return result
            result.status = "converged"
            result.reason = "unfinished recovery verified converged"
        else:
            result.status = "restore-needed"
            result.reason = "unfinished recovery verified converged; dry-run retains checkpoint"
            result.plan = []
        log(f"{result.status}: {result.reason}")
        return result

    intent_started = False
    mutation_possible = recovering
    # A configuration POST went out in THIS run (an allocation Clear or a pass step), whatever the
    # answer; `mutation_possible` also carries the unfinished recovery of earlier runs.
    sent = False

    def prepare_mutation() -> bool:
        nonlocal intent_started
        if intent_started:
            return True
        if not checkpoint_action("begin"):
            if checkpoint is not None and not recovering:
                # Publication may have replaced the file before its directory fsync failed.
                # No mutation was attempted, so remove only this invocation's new intent.
                checkpoint_action("finish", secondary="unused intent cleanup")
            return False
        intent_started = True
        return True

    def discard_unused_intent() -> bool:
        nonlocal intent_started
        if intent_started and not mutation_possible:
            if not checkpoint_action("finish"):
                return False
            intent_started = False
        return True

    def count_failure(shape: Mapping[str, Any], *, structural: bool = False) -> AutorestoreResult:
        """Record this run's failure against the intent; the RETRY_LIMIT-th identical one is an error.

        Only a run that sent (or may have sent) a configuration POST, or whose failure is structural
        (the gateway answered with something that is not the page), counts: a fault before any send
        changed nothing, so it neither increments nor resets the counter and keeps the intent."""
        record = getattr(checkpoint, "record_failure", None)
        if record is None or not (sent or structural):
            return result
        try:
            count = record(_failure_fingerprint(shape))
        except OSError as exc:
            result.status = "error"
            result.reason = f"{result.reason}; recovery failure count update failed: {exc}"
            log(f"error: {result.reason}")
            return result
        if count <= 0:
            return result
        if count >= RETRY_LIMIT:
            result.status = "error"
            result.reason = (
                f"{result.reason}; the same failure ended {count} consecutive runs: automatic recovery "
                f"stops writing; verify the gateway, then run `bgwcli restore` with the dump or remove "
                f"{getattr(checkpoint, 'path', 'the recovery intent')} to let the timer try again"
            )
        else:
            result.reason = f"{result.reason} (failure {count}/{RETRY_LIMIT} of this recovery)"
        log(f"{result.status}: {result.reason}")
        return result

    def stop_for_deadline(before: str) -> AutorestoreResult:
        """End the run because its time limit passed before the first write (`before` names it).

        Nothing sent in this run: nothing is counted, no closing read is made, an unfinished recovery
        intent of an earlier run is kept and an intent this run just prepared is discarded. After this
        run's own allocation Clear that write is counted and verified (by the post-Clear refresh) like the
        end-of-run limit path, unless that refresh shows the gateway converged: then the run ends
        converged and nothing is counted.
        Callers use it only when no restore pass has sent anything: the only write that can already have
        gone out is the Clear, so the wording below is built from what was sent, not from the call site."""
        result.status = "not-converged"
        result.reason = (
            f"the run time limit of {options.max_run_seconds:g}s was reached before {before}; "
            + (
                "the allocation Clear was already sent; no restore step was started"
                if sent else "nothing was sent"
            )
            + "; the next timer run will retry"
        )
        if not sent:
            if not discard_unused_intent():
                return result
            log(f"not-converged: {result.reason}")
            return result
        structural = False
        try:
            if result.final_diff is None:
                # No read follows the Clear (not reachable from the call sites below, which run after
                # the post-Clear refresh): the closing read after a sent write is still made.
                closing, failures = fetch_pages(client, page_ids)
                structural = any(failure.structural for failure in failures)
                if failures:
                    raise RouterConnectionError(_failure_reason(failures))
                result.final_diff = snapshot(closing)
            # Otherwise result.final_diff is the post-Clear refresh: it is the closing read, so the
            # pages are not read a second time.
        except BgwError as error:
            result.final_diff = None
            result.session_pool_full = isinstance(error, RouterSessionPoolFullError)
            if result.session_pool_full:
                result.waited_ms, result.retry_count = pool_full_metadata(error)
            result.status = "error"
            result.reason = (
                f"{result.reason}; the closing verification read failed after the allocation Clear: {error}"
            )
            log(f"error: {result.reason}")
            return count_failure(
                {"kind": "verification", "step": None, "error": type(error).__name__}, structural=structural
            )
        # Only a refresh that shows the gateway converged ends the run converged; remaining differences
        # that no step can execute are not-converged here exactly as on the ordinary path.
        if restore_converged(result.final_diff, False):
            if not checkpoint_action("finish"):
                return result
            result.status = "converged"
            result.reason = (
                f"the run time limit of {options.max_run_seconds:g}s was reached before {before}; "
                "the allocation Clear was already sent and the refreshed read shows the gateway converged"
            )
            log(f"converged: {result.reason}")
            return result
        log(f"not-converged: {result.reason}")
        return count_failure({"kind": "not-converged", "open": _open_differences(result.final_diff)})

    if options.commit and recovering:
        try:
            stopped = getattr(checkpoint, "failure_count", lambda: 0)()
        except OSError as exc:
            stopped, result.reason = 0, f"{result.reason}; recovery failure count read failed: {exc}"
            result.status = "error"
            log(f"error: {result.reason}")
            return result
        if stopped >= RETRY_LIMIT:
            result.status = "error"
            result.reason = (
                f"automatic recovery stopped: the same failure ended {stopped} consecutive runs; nothing "
                f"sent. Verify the gateway, then run `bgwcli restore` with the dump or remove "
                f"{getattr(checkpoint, 'path', 'the recovery intent')} to let the timer try again ({reason})"
            )
            log(f"error: {result.reason}")
            return result

    pending = pending_allocation_requests(dump, parsed, options.pages)
    if pending:
        try:
            preflight = inspect_allocation_conflicts(
                client, pending,
                **({"snapshot_pages": parsed} if isinstance(parsed, OwnershipSnapshotPages) else {}),
            )
        except BgwError as exc:
            result.allocation_preflight = allocation_preflight_report(error=exc)
            result.session_pool_full = isinstance(exc, RouterSessionPoolFullError)
            if result.session_pool_full:
                result.waited_ms, result.retry_count = pool_full_metadata(exc)
            unreachable = isinstance(exc, (RouterConnectionError, RouterResponseError))
            result.status = "router-unreachable" if unreachable else "error"
            result.reason = f"allocation preflight failed: {exc}"
            log(f"{result.status}: {result.reason}")
            return result
        if preflight.conflicts:
            result.allocation_preflight = allocation_preflight_report(preflight)
            blocked = result.allocation_preflight["planStatus"] == "blocked"
            if not options.commit:
                result.status = "error" if blocked else "restore-needed"
                result.reason = result.allocation_preflight["reason"]
                log(f"{result.status}: {result.reason}")
                for conflict in preflight.conflicts:
                    log(f"allocation conflict: {conflict.ip} is held by {conflict.holder_mac}")
                return result
            if not blocked and out_of_time():
                # The snapshot reads of a slow gateway used up the run limit: the Clear and Rescan is a
                # write like any restore step and is not started after it. Nothing was sent, so nothing
                # is counted, an existing recovery intent is kept and no closing read is made.
                return stop_for_deadline("the allocation Clear")
            if not blocked and not prepare_mutation():
                return result
            if not blocked and out_of_time():
                # Checkpoint publication (a blocking fsync) used up the limit: re-checked right
                # before the Clear starts. The intent just prepared is discarded, an earlier one kept.
                return stop_for_deadline("the allocation Clear")
            rescan_evidence = AllocationRescanEvidence()
            try:
                result.final_diff = None
                rescan_allocation_conflicts(client, preflight, log=log, evidence=rescan_evidence)
                mutation_possible = sent = True
                result.allocation_preflight = allocation_preflight_report(preflight, rescan=rescan_evidence)
                parsed, failures = fetch_pages(client, page_ids)
                if failures:
                    raise RouterConnectionError(f"could not refresh restore pages: {_failure_reason(failures)}")
                diff = snapshot(parsed)
                result.final_diff = diff
            except BgwError as exc:
                clear_possible = rescan_evidence.clear_attempted is not False
                mutation_possible = mutation_possible or clear_possible
                sent = sent or clear_possible
                result.allocation_preflight = allocation_preflight_report(preflight, error=exc, rescan=rescan_evidence)
                result.session_pool_full = isinstance(exc, RouterSessionPoolFullError)
                if result.session_pool_full:
                    result.waited_ms, result.retry_count = pool_full_metadata(exc)
                # A connection or HTTP fault before the Clear was sent (its nonce read) wrote
                # nothing, so it is the same quiet "router-unreachable" as a failed snapshot fetch.
                # A refused nonce page (the devices page answering without its form) is the gateway
                # answering with something that is not the page: structural, never "unreachable".
                refused = isinstance(exc, NoncePageRefusedError)
                unreachable = (
                    rescan_evidence.clear_attempted is False
                    and isinstance(exc, (RouterConnectionError, RouterResponseError))
                    and not refused
                )
                result.status = "router-unreachable" if unreachable else "error"
                result.reason = f"allocation preflight failed: {exc}"
                log(f"{result.status}: {result.reason}")
                if not discard_unused_intent():
                    return result
                if refused or (rescan_evidence.clear_attempted is not False and intent_started):
                    # A Clear and Rescan went out and the conflict did not resolve: the next run would
                    # post it again, so this failure counts toward the cross-run limit too. A refused
                    # nonce page is structural and counts like any other structural failure.
                    return count_failure({
                        "kind": "allocation",
                        "conflicts": sorted((c.ip, c.target_mac, c.holder_mac) for c in preflight.conflicts),
                        "error": type(exc).__name__,
                    }, structural=refused)
                return result

    steps = build_restore_plan(diff, dump, parsed, restore_options)
    result.plan = steps
    if result.allocation_preflight is not None:
        result.allocation_preflight.update(
            planStatus="complete", planComplete=True,
            reason="Allocation ownership verified; restore plan built from refreshed pages.",
        )
    if not options.commit:
        result.status = "restore-needed"
        log(f"restore-needed: {reason}; {len(steps)} steps planned (dry-run, nothing sent)")
        return result

    log(f"factory reset detected: {reason}")
    if out_of_time():
        # The snapshot reads of a slow gateway used up the whole run limit: the first step is held to
        # it exactly like a step between two others. Nothing was sent, so nothing is counted and an
        # existing recovery intent is kept; the initial read stays the latest known state. When the
        # allocation preflight already sent its Clear, that write is counted and verified like the
        # writes of the end-of-run limit path.
        return stop_for_deadline("the first restore step")
    # (page, kind, entry) -> the pass whose POST the gateway acknowledged (see _without_reposts).
    acknowledged: dict[tuple[str, ...], int] = {}
    deadline_reached = False
    for number in range(1, max(1, options.max_passes) + 1):
        if number > 1:
            if out_of_time():
                # The inter-pass sleep overran the limit: pass N+1 starts no write. Pass N's closing
                # read and its counted writes stay; the run ends like the end-of-run limit path.
                deadline_reached = True
                log(f"run time limit of {options.max_run_seconds:g}s reached after pass {number - 1}; no further pass")
                break
            steps = _without_reposts(build_restore_plan(diff, dump, parsed, restore_options), acknowledged)
            result.plan = steps
        log(f"pass {number}/{options.max_passes}: {len(steps)} steps")
        # Read-only planning and refusals must not authorize recovery on a later invocation.
        executable = any(
            step.kind != "skip"
            and (step.deferred is not None or (step.blocked is None and step.raw_payload is not None))
            for step in steps
        )
        if executable and not prepare_mutation():
            return result
        if executable and out_of_time():
            # Planning, progress logging or checkpoint publication used up the limit: re-checked right
            # before this pass's first step starts, on every pass (the first checkpoint of a run can
            # be published in a later pass when the earlier ones were entirely blocked).
            if not sent or not result.passes:
                # Nothing was sent in the run, or only the allocation Clear: the shared stop path
                # words and settles it from what was actually sent.
                return stop_for_deadline("the first restore step" if number == 1 else "the next restore step")
            # Earlier passes sent writes: end like the between-pass limit, keeping the last closing
            # diff, counting the sent writes and keeping the intent.
            deadline_reached = True
            log(
                f"run time limit of {options.max_run_seconds:g}s reached before pass {number}'s first step; "
                "earlier writes in this run were sent; no further step"
            )
            break
        completed: list[RestoreStepResult] = []

        def record_step(
            step_result: RestoreStepResult,
            completed: list[RestoreStepResult] = completed,
            total: int = len(steps),
        ) -> None:
            completed.append(step_result)
            if on_step is not None:
                on_step(step_result)
            if len(completed) < total and out_of_time():
                raise _RunDeadlineReached

        try:
            execution = execute_restore(client, steps, record_step)
        except _RunDeadlineReached:
            # The run's time is up between two steps: what was sent stays counted and is verified
            # below; the steps that never started are simply not run, and no further pass begins.
            deadline_reached = True
            execution = RestoreExecution(steps=completed, stopped_at=None)
        for done in execution.steps:
            if _acknowledged(done):
                acknowledged.setdefault(_step_identity(done), number)
        facts = _ExecutionFacts.from_steps(execution.steps)
        mutation_possible = mutation_possible or facts.mutation_possible
        sent = sent or facts.mutation_possible
        if unanswered_write_step(execution.steps) is not None:
            # Evidence from the executed steps, before any verification read can fail: every exit
            # below (and the retry-limit one) keeps it.
            result.write_unanswered = True
        # Execution evidence exists before the verification read, which can itself fail.
        summary = _pass_summary(number, execution, None)
        result.passes.append(summary)
        result.final_diff = None
        failed_step = facts.failed_step
        result.session_pool_full = facts.pool_step is not None
        result.waited_ms = facts.pool_step.waited_ms if facts.pool_step is not None else None
        result.retry_count = facts.pool_step.retry_count if facts.pool_step is not None else None
        if not discard_unused_intent():
            return result
        reconnect = facts.reconnect_step
        if reconnect is not None:
            result.status = "error"
            result.reason = reconnect_verification_reason(reconnect)
            log(f"error: {result.reason}")
            if reconnect.write_response_received is True or reconnect.acknowledgement_observed is True:
                # The gateway answered the LAN write: the move is the expected outcome, not a repeating
                # failure, and a count keyed to the old origin would only pile up across resets.
                return result
            return count_failure({"kind": "reconnect", "step": [*_step_identity(reconnect), reconnect.status]})
        fault = (failed_step.error_type or "") if failed_step is not None else ""
        transport_fault = fault in {"RouterConnectionError", "RouterResponseError"}
        lost_session = failed_step is not None and (
            failed_step.session_pool_full or (fault == "RouterAuthError" and not failed_step.error_page_level)
        )
        if failed_step is not None and not lost_session and transport_fault:
            detail = (failed_step.error or "restore execution stopped").rstrip(". ")
            if failed_step.write_attempted is False and not sent:
                # Nothing was sent in this run: the gateway is unreachable, which the timer treats as
                # quiet. No further pass, no sleep, nothing counted, the intent stays.
                result.status = "router-unreachable"
                result.reason = (
                    f"pass {number} stopped at step {failed_step.order}: nothing was sent: {detail}; "
                    "no further pass in this run; the next timer run will retry"
                )
                log(f"{result.status}: {result.reason}")
                return result
        # A transport fault before this step's POST after this run already sent a write: the run
        # ends here like every other pre-send fault (no further pass, no sleep). The closing read
        # below still decides what the sent writes did; the run is not-converged and counted.
        pre_send_transport_end = (
            failed_step is not None and not lost_session and transport_fault
            and failed_step.write_attempted is False and sent
        )
        if failed_step is not None and failed_step.acknowledgement_observed is True and fault:
            # The gateway acknowledged the write and the state read after it failed (a transport
            # fault, a lost session, a refused page, a full pool): the write stands, so this counts
            # toward the limit, and the closing read is not attempted.
            detail = (failed_step.error or "restore execution stopped").rstrip(". ")
            result.status = "error"
            result.reason = (
                f"pass {number} stopped at step {failed_step.order}: verification-unavailable: the "
                f"{failed_step.page} write was acknowledged but its state read failed: {detail}; "
                "no further pass in this run; the next timer run will re-read the gateway"
            )
            log(f"error: {result.reason}")
            return count_failure({
                "kind": "verification",
                "step": [*_step_identity(failed_step), failed_step.status],
                "error": fault,
            })
        if (
            failed_step is not None
            and failed_step.write_attempted is False
            and (fault or failed_step.session_pool_full)
            and not transport_fault
        ):
            # A fault before this step's POST that is not a transport one (a lost session, a refused
            # page, a full pool, a structural failure): the plan, the session and the snapshot it was
            # built from can no longer be trusted, so the run ends here - no replanning from the stale
            # snapshot, no sleep, no second login handshake. Counted only when a POST was sent in this
            # run or the fault is a structural nonce-page refusal, and then only against an existing
            # recovery intent; the recovery intent is kept either way.
            result.status = "error"
            result.reason = (
                f"pass {number} stopped at step {failed_step.order}: {_pre_send_fault_label(failed_step)}: "
                f"{(failed_step.error or 'restore execution stopped').rstrip('. ')}; "
                "no further pass in this run; the next timer run will re-read the gateway and retry"
            )
            log(f"error: {result.reason}")
            return count_failure({
                "kind": "pre-send",
                "step": [*_step_identity(failed_step), failed_step.status],
                "error": fault,
            }, structural=fault == "NoncePageRefusedError")
        structural = False
        diff_read = not lost_session  # a full pool or a lost session ends the run: no second login
        if diff_read:
            try:
                parsed, failures = fetch_pages(client, page_ids)
                structural = any(failure.structural for failure in failures)
                if failures:
                    raise RouterConnectionError(_failure_reason(failures))
                diff = snapshot(parsed)
            except BgwError as error:
                result.session_pool_full = result.session_pool_full or isinstance(error, RouterSessionPoolFullError)
                if isinstance(error, RouterSessionPoolFullError):
                    result.waited_ms, result.retry_count = pool_full_metadata(error)
                result.status = "error"
                evidence = (
                    "router write attempted but" if facts.mutation_possible
                    else "no final configuration write performed;"
                )
                result.reason = f"pass {number}: {evidence} verification unavailable: {error}"
                log(f"error: {result.reason}")
                # A write went out and its effect cannot be read back (the next run would send it again),
                # or the gateway answered with something that is not the page; a plain fault with nothing
                # sent is not counted.
                return count_failure({
                    "kind": "verification",
                    "step": [*_step_identity(failed_step), failed_step.status] if failed_step is not None else None,
                    "error": type(error).__name__,
                }, structural=structural)
        result.final_diff = diff if diff_read else None
        converged = failed_step is None and restore_converged(diff, False)
        summary["converged"] = converged
        log(
            f"pass {number}/{options.max_passes}: {summary['applied']} applied, {summary['unchanged']} unchanged, "
            f"{summary['blocked']} blocked, "
            f"{summary['failed']} failed, {summary['notRun']} not run"
            + (f"; stopped at step {execution.stopped_at}" if execution.stopped_at is not None else "")
        )
        # Only an explicit failure before any configuration POST may retry in a later pass. A sent
        # (or possibly sent) write is never re-posted, even when the gateway answered that it did not
        # perform it ("No changes detected" with the page in another state, a rejection banner).
        if failed_step is not None and (failed_step.write_attempted is not False or pre_send_transport_end):
            stopped_at = failed_step.order
            detail = (failed_step.error or "restore execution stopped").rstrip(". ")
            if unanswered_write_step([failed_step]) is not None:
                # No answer to a write is never "not converged": the gateway may or may not have
                # acted, and a readable closing diff cannot settle that.
                result.status = "error"
                result.write_unanswered = True
                # A refused Continue after an answered Save is still an unconfirmed write, but its Save
                # did get an answer: the text says so (the writeUnanswered flag is unchanged).
                outcome = (
                    "the Save was answered and the Continue was not posted"
                    if CONTINUE_NOT_POSTED in (failed_step.error or "") else "got no answer"
                )
                result.reason = (
                    f"pass {number} stopped at step {stopped_at}: the {failed_step.page} write "
                    f"({failed_step.description}) {outcome}: {detail}; "
                    "no further pass in this run; the next timer run will re-read the gateway and retry"
                )
            else:
                result.status = "not-converged"
                result.reason = (
                    f"pass {number} stopped at step {stopped_at}: {detail}; "
                    "no further pass in this run; the next timer run will re-read the gateway and retry"
                )
            log(f"{result.status}: {result.reason}")
            return count_failure({
                "kind": "step",
                "step": [*_step_identity(failed_step), failed_step.status],
                "open": _open_differences(result.final_diff),
            })
        if converged:
            if not checkpoint_action("finish"):
                return result
            result.status = "converged"
            result.reason = f"converged after {number} pass{'es' if number > 1 else ''}"
            log(f"converged after pass {number}")
            return result
        if number < options.max_passes:
            wait = options.wait_seconds if facts.mutation_possible or failed_step is not None else 0
            if deadline_reached or out_of_time(wait):
                deadline_reached = True
                log(f"run time limit of {options.max_run_seconds:g}s reached after pass {number}; no further pass")
                break
            if facts.mutation_possible or failed_step is not None:
                log(f"not yet converged; waiting {options.wait_seconds}s before pass {number + 1}")
                pause(options.wait_seconds)
            else:
                # Nothing was sent and nothing failed (only blocked or skipped steps): there is no
                # gateway state to wait for.
                log(f"not yet converged; nothing was sent in pass {number}, so pass {number + 1} starts at once")

    result.status = "not-converged"
    result.reason = (
        f"not converged after {len(result.passes)} pass{'es' if len(result.passes) > 1 else ''}"
        + (f"; the run time limit of {options.max_run_seconds:g}s was reached" if deadline_reached else "")
        + ("; acknowledged writes were not re-posted" if acknowledged else "")
        + "; the next timer run will retry"
    )
    log(f"not-converged: {result.reason}")
    if intent_started or recovering:
        return count_failure({"kind": "not-converged", "open": _open_differences(result.final_diff)})
    return result
