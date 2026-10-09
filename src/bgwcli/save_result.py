"""One save-outcome -> exit code / message contract for every configuration write path.

The Wi-Fi, LAN and generic `set`/`submit` paths observe the same gateway outcomes through different
code (execute_restore vs confirm_saved_page); this module is the single place that turns an observed
outcome into the exit code and message, so the same outcome never maps to different codes.

| Gateway outcome                                        | Exit | Outcome     |
| "Changes saved" observed, post-save verify passes      | 0    | applied     |
| "Changes saved" observed, live form differs            | 1    | applied     |
| "Changes saved" observed, re-read attempted but failed | 2    | applied     |
| "Changes saved" observed, re-read skipped by design    | 0    | applied     |
| "No changes detected", live form equals the request    | 0    | unchanged   |
| "No changes detected", live form differs               | 1    | failed      |
| "No changes detected", nothing requested to compare    | 0    | unchanged   |
| "No changes detected", live form could not be re-read  | 2    | failed      |
| Router error banner                                    | 1    | failed      |
| No acknowledgement within the timeout                  | 2    | failed      |

A page with no acknowledgement protocol (no Save control) whose POST was answered 2xx/3xx follows the
"Changes saved" rows: its live re-read decides 0 / 1 / 2 exactly the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .exit_codes import NEGATIVE_ANSWER, NO_ANSWER
from .restore import VERIFY_BEFORE_RETRYING, WRITE_REJECTED_PREFIX, sent_guidance

NO_CHANGES_TEXT = "No changes detected. Save not performed"
UNCONFIRMED_TEXT = f"The change could not be confirmed; {VERIFY_BEFORE_RETRYING}"


@dataclass(frozen=True)
class SaveDecision:
    exit_code: int
    outcome: str
    committed: bool
    message: str | None
    # True when the message states a verified fact (exit 0 result text) rather than a warning.
    positive: bool


def _differences(mismatches: Mapping[str, Mapping[str, str]] | None) -> str:
    if not mismatches:
        return ""
    return ", ".join(
        f"{name} (wanted {pair.get('wanted', '')!r}, live {pair.get('live', '<absent>')!r})"
        for name, pair in mismatches.items()
    )


def observed_no_change(status: str, write_performed: bool | None, write_attempted: bool | None) -> bool:
    """True when `write_performed is False` is the gateway's "No changes detected" answer. A step
    that failed before its POST (a refused nonce page) also carries `write_performed False` as
    truthful evidence, but nothing was sent, so nothing was answered."""
    return write_performed is False and not (status == "failed" and write_attempted is False)


def decide_save_result(
    status: str,
    error: str | None,
    write_performed: bool | None,
    verified: bool | None,
    mismatches: Mapping[str, Mapping[str, str]] | None = None,
    *,
    requested: bool,
    verify_warning: str | None = None,
    write_attempted: bool | None = None,
    write_attempts: int | None = None,
) -> SaveDecision:
    """Map one observed save outcome to (exit code, outcome, committed, message).

    `status`/`error`/`write_performed` come from the confirmation step (RestoreStepResult);
    `verified`/`mismatches` from the post-save live re-read (None when it did not run or failed);
    `requested` says whether the command asked for field values the re-read can compare;
    `verify_warning` is set only when a re-read was attempted and failed (None when skipped by design).
    """
    if error is not None and error.startswith(WRITE_REJECTED_PREFIX):
        return SaveDecision(NEGATIVE_ANSWER, "failed", False, error, False)
    if status == "applied":
        if verified is False:
            return SaveDecision(NEGATIVE_ANSWER, "applied", True, None, False)
        if verified is None and verify_warning:
            # The re-read was attempted and could not complete (retries exhausted, page-level 4xx,
            # unparsable page): the write is committed but its result is unknown - "could not answer".
            return SaveDecision(NO_ANSWER, "applied", True, None, False)
        return SaveDecision(0, "applied", True, None, False)
    if observed_no_change(status, write_performed, write_attempted):
        if verified is True:
            return SaveDecision(
                0, "unchanged", False,
                f"{NO_CHANGES_TEXT}; the live form was re-read and already matches the requested values.", True,
            )
        if verified is False:
            return SaveDecision(
                NEGATIVE_ANSWER, "failed", False,
                f"{NO_CHANGES_TEXT}; the router ignored or normalised the change: {_differences(mismatches)}. "
                f"{sent_guidance(write_attempts)}", False,
            )
        if not requested:
            return SaveDecision(0, "unchanged", False, f"{NO_CHANGES_TEXT}.", True)
        detail = f": {verify_warning}" if verify_warning else ""
        # A warning that already ends with the guidance sentence is not given it a second time.
        guidance = "" if detail.endswith(VERIFY_BEFORE_RETRYING) else f"; {VERIFY_BEFORE_RETRYING}"
        return SaveDecision(
            NO_ANSWER, "failed", False,
            f"{NO_CHANGES_TEXT}. The requested state could not be verified{detail}{guidance}", False,
        )
    # Every remaining step is failed or reconnect-required: an "unchanged" step always carries
    # write_performed False (restore._save_outcome) and is decided above.
    return SaveDecision(NO_ANSWER, status, False, error or UNCONFIRMED_TEXT, False)


def decision_for_step(step: Any, verified: bool | None = None, mismatches: Any = None, *,
                      requested: bool, verify_warning: str | None = None) -> SaveDecision:
    """Convenience wrapper over a RestoreStepResult-like object."""
    return decide_save_result(
        step.status, step.error, step.write_performed, verified, mismatches,
        requested=requested, verify_warning=verify_warning,
        write_attempted=getattr(step, "write_attempted", None),
        write_attempts=getattr(step, "write_attempts", None),
    )
