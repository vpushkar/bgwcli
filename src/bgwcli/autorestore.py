"""Unattended factory-reset recovery: detect that the gateway lost the dump's configuration and put
it back, without ever acting on ordinary drift.

Designed for a systemd timer (see deploy/): every run fetches the same pages `diff`/`restore` use,
diffs them against the baseline dump and asks `detect_factory_reset` whether the difference looks
like a reset (EVERY dumped service, forward and reservation gone) rather than a deliberate manual
change (some entries gone, values edited). Only a reset triggers `restore` passes, and those never
prune: router-only entries are always left alone. The access-code fallback (the printed sticker code
comes back after a reset) lives in cli.py; this module only records that it was used and treats it
as a reset signal on its own.

Exit-code contract (AutorestoreResult.exit_code): 0 when there was nothing to do or the restore
converged or the router is unreachable (timers must stay quiet), 1 when a restore is needed
(dry-run) or did not converge, 2 when the command could not answer after writing to the router.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from . import format as fmt
from .errors import RouterConnectionError
from .fetch import ParsedPageResult
from .restore import (
    RestoreOptions,
    RestoreStep,
    RestoreStepResult,
    build_restore_plan,
    execute_restore,
    restore_converged,
)
from .snapshot import OPTIONAL_FORM_PAGES, Snapshot, extract_snapshot, snapshot_pages_for
from .snapshot_diff import SnapshotDiff, diff_snapshots
from .types import ParsedPage, to_json_dict

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
_sleep = time.sleep  # seam for tests (cli passes nothing; run_autorestore(sleep=...) overrides)

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
        missing["forms"] = sum(1 for page in dumped_forms if page in diff.forms)
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


@dataclass(frozen=True)
class AutorestoreOptions:
    commit: bool
    max_passes: int = 3
    wait_seconds: int = 120
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
    return out


def _pass_summary(number: int, execution: Any, converged: bool) -> dict[str, Any]:
    counts = {status: 0 for status in ("applied", "blocked", "failed", "skipped", "not-run")}
    for step in execution.steps:
        counts[step.status] = counts.get(step.status, 0) + 1
    return {
        "pass": number,
        "applied": counts["applied"],
        "blocked": counts["blocked"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "notRun": counts["not-run"],
        "stoppedAt": execution.stopped_at,
        "converged": converged,
    }


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
) -> AutorestoreResult:
    """Login -> fetch -> diff -> detect; then (commit only) restore passes until converged.

    Anything that goes wrong before the first POST is reported as "router-unreachable" (exit 0, one
    log line): the gateway's web UI hangs routinely and a timer must not page on that. After a POST
    the router has been written to, so a failed re-read is an "error" (exit 2)."""
    pause = sleep if sleep is not None else _sleep
    optional = tuple(page for page in OPTIONAL_FORM_PAGES if page in dump.forms)
    page_ids = snapshot_pages_for(optional)
    restore_options = RestoreOptions(prune=False, include_secrets=False, pages=options.pages)

    def snapshot(parsed: Mapping[str, ParsedPage]) -> SnapshotDiff:
        live = extract_snapshot(parsed, ts=ts, router_host=router_host, include=optional)
        return diff_snapshots(dump, live, pages=options.pages)

    try:
        client, used_fallback = client_factory()
        parsed, failures = fetch_pages(client, page_ids)
    except RouterConnectionError as error:
        log(f"router-unreachable: {error}")
        return AutorestoreResult(status="router-unreachable", reason=str(error))
    if failures:
        reason = _failure_reason(failures)
        log(f"router-unreachable: {reason}")
        return AutorestoreResult(status="router-unreachable", reason=reason)

    diff = snapshot(parsed)
    detected, reason = detect_factory_reset(diff, dump, on_any_diff=options.on_any_diff, pages=options.pages)
    if used_fallback:
        log(FALLBACK_CODE_REASON)
        detected, reason = True, f"{FALLBACK_CODE_REASON}; {reason}"
    signature = ResetSignature.from_diff(diff, dump, pages=options.pages)
    result = AutorestoreResult(
        status="no-reset",
        reason=reason,
        detected=detected,
        used_fallback_code=used_fallback,
        missing=signature.missing,
        final_diff=diff,
    )
    if not detected:
        log(f"no-reset: {reason}")
        return result

    steps = build_restore_plan(diff, dump, parsed, restore_options)
    result.plan = steps
    if not options.commit:
        result.status = "restore-needed"
        log(f"restore-needed: {reason}; {len(steps)} steps planned (dry-run, nothing sent)")
        return result

    log(f"factory reset detected: {reason}")
    for number in range(1, max(1, options.max_passes) + 1):
        if number > 1:
            steps = build_restore_plan(diff, dump, parsed, restore_options)
            result.plan = steps
        log(f"pass {number}/{options.max_passes}: {len(steps)} steps")
        execution = execute_restore(client, steps, on_step)
        parsed, failures = fetch_pages(client, page_ids)
        if failures:
            result.status = "error"
            result.reason = (
                f"pass {number}: router written to but pages could not be re-read: {_failure_reason(failures)}"
            )
            result.passes.append(_pass_summary(number, execution, False))
            log(f"error: {result.reason}")
            return result
        diff = snapshot(parsed)
        result.final_diff = diff
        converged = execution.stopped_at is None and restore_converged(diff, False)
        summary = _pass_summary(number, execution, converged)
        result.passes.append(summary)
        log(
            f"pass {number}/{options.max_passes}: {summary['applied']} applied, {summary['blocked']} blocked, "
            f"{summary['failed']} failed, {summary['notRun']} not run"
            + (f"; stopped at step {execution.stopped_at}" if execution.stopped_at is not None else "")
        )
        if converged:
            result.status = "converged"
            result.reason = f"converged after {number} pass{'es' if number > 1 else ''}"
            log(f"converged after pass {number}")
            return result
        if number < options.max_passes:
            log(f"not yet converged; waiting {options.wait_seconds}s before pass {number + 1}")
            pause(options.wait_seconds)

    result.status = "not-converged"
    result.reason = f"not converged after {len(result.passes)} pass{'es' if len(result.passes) > 1 else ''}"
    log(f"not-converged: {result.reason}")
    return result
