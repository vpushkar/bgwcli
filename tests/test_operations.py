"""Ported from tests/operations.test.ts, with committed-shape and JSON key coverage from cli.ts usage."""

from __future__ import annotations

from dataclasses import dataclass, field

from bgwcli.actions import RouterAction, get_action
from bgwcli.operations import (
    OperationResult,
    action_committed,
    action_dry_run,
    diagnostic_committed,
    diagnostic_dry_run,
    quote_arg,
    restore_committed,
    restore_dry_run,
    set_committed,
    set_dry_run,
    submit_committed,
    submit_dry_run,
)
from bgwcli.types import ParsedButton, to_json_dict


@dataclass
class _Plan:
    """Stand-in for mutations.MutationPlan (owned by another worker)."""

    page: str
    blocked: bool
    raw_payload: dict[str, str]
    display_payload: dict[str, str]
    display_changes: dict[str, str]
    button: ParsedButton | None = None
    reason: str | None = None


@dataclass
class _Step:
    """Stand-in for restore.RestoreStep."""

    order: int
    kind: str
    page: str
    description: str
    button: str | None = None
    raw_payload: dict[str, str] | None = None
    blocked: str | None = None


@dataclass
class _StepResult:
    order: int
    page: str
    kind: str
    description: str
    status: str
    status_code: int | None = None
    error: str | None = None


@dataclass
class _Execution:
    steps: list[_StepResult] = field(default_factory=list)
    stopped_at: int | None = None


PLAN = _Plan(
    page="wconfig_unified",
    blocked=False,
    raw_payload={"nonce": "abc", "ssid": "Home"},
    display_payload={"nonce": "[redacted]", "ssid": "Home"},
    display_changes={"ssid": "Home"},
)


def test_action_dry_run_returns_consistent_guarded_operation_shape():
    action = RouterAction(
        name="run-speed-test",
        page="speed",
        description="Run speed test",
        confirm_token="SPEED",
        payload={"run": "Run Speed Test"},
        dangerous=False,
    )
    result = action_dry_run(action, {"run": "Run Speed Test"})
    assert to_json_dict(result) == {
        "operation": "action",
        "dryRun": True,
        "committed": False,
        "page": "speed",
        "action": "run-speed-test",
        "guarded": True,
        "dangerous": False,
        "confirmation": "SPEED",
        "commitCommand": "action run-speed-test --commit --confirm SPEED",
        "payload": {"run": "Run Speed Test"},
    }


def test_action_committed_carries_dangerous_flag_status_and_location():
    restart = get_action("restart")
    result = action_committed(restart, 302, "/cgi-bin/home.ha")
    assert result == OperationResult(
        operation="action",
        dry_run=False,
        committed=True,
        page="restart",
        guarded=True,
        dangerous=True,
        confirmation="RESTART",
        action="restart",
        status_code=302,
        location="/cgi-bin/home.ha",
    )
    assert action_committed(restart, 200, None).location is None


def test_set_dry_run_includes_confirmation_only_for_guarded_pages():
    guarded = set_dry_run(PLAN, "WCONFIG-UNIFIED")
    assert guarded.operation == "set"
    assert guarded.dry_run is True
    assert guarded.committed is False
    assert guarded.guarded is True
    assert guarded.dangerous is False
    assert guarded.confirmation == "WCONFIG-UNIFIED"
    assert guarded.commit_command == "set wconfig_unified KEY=VALUE... --commit --confirm WCONFIG-UNIFIED"
    assert guarded.changes == {"ssid": "Home"}
    assert guarded.payload == {"nonce": "[redacted]", "ssid": "Home"}

    unguarded = set_dry_run(PLAN, None)
    assert unguarded.guarded is False
    assert unguarded.confirmation is None
    assert "confirmation" not in to_json_dict(unguarded)
    assert unguarded.commit_command == "set wconfig_unified KEY=VALUE... --commit"


def test_set_dry_run_quotes_unsafe_page_and_confirmation():
    plan = _Plan(page="weird page", blocked=False, raw_payload={}, display_payload={}, display_changes={})
    result = set_dry_run(plan, "WEIRD PAGE")
    assert result.commit_command == "set 'weird page' KEY=VALUE... --commit --confirm 'WEIRD PAGE'"


def test_set_committed_shape():
    result = set_committed("dosprotect", 302, None)
    assert to_json_dict(result) == {
        "operation": "set",
        "dryRun": False,
        "committed": True,
        "page": "dosprotect",
        "guarded": True,
        "dangerous": False,
        "statusCode": 302,
    }


def test_submit_dry_run_and_diagnostic_dry_run_include_commit_commands():
    plan = _Plan(
        page=PLAN.page,
        blocked=PLAN.blocked,
        raw_payload=PLAN.raw_payload,
        display_payload=PLAN.display_payload,
        display_changes=PLAN.display_changes,
        button=ParsedButton(
            name="Enable", type="submit", value="Enable Packet Filters", label="Enable Packet Filters", sensitive=False
        ),
    )
    submit = submit_dry_run(plan, "Enable", "PACKETFILTER")
    assert submit.operation == "submit"
    assert submit.button == "Enable Packet Filters"
    assert submit.commit_command == "submit wconfig_unified Enable --commit --confirm PACKETFILTER"
    assert submit.confirmation == "PACKETFILTER"
    assert submit.guarded is True
    assert submit.changes == PLAN.display_changes
    assert submit.payload == PLAN.display_payload

    # Without a resolved button, the requested name is echoed back.
    assert submit_dry_run(PLAN, "Save", "WCONFIG-UNIFIED").button == "Save"

    diag = diagnostic_dry_run("ping", "example.com", {"Ping": "Ping"}, "Ping")
    assert diag.operation == "diagnostic"
    assert diag.page == "diag"
    assert diag.action == "ping"
    assert diag.target == "example.com"
    assert diag.button == "Ping"
    assert diag.confirmation == "DIAG"
    assert diag.commit_command == "diagnostics ping example.com --commit --confirm DIAG"
    assert diag.payload == {"Ping": "Ping"}


def test_submit_committed_and_diagnostic_committed_shapes():
    submit = submit_committed("packetfilter", "Enable", 302, "/cgi-bin/packetfilter.ha")
    assert to_json_dict(submit) == {
        "operation": "submit",
        "dryRun": False,
        "committed": True,
        "page": "packetfilter",
        "button": "Enable",
        "guarded": True,
        "dangerous": False,
        "statusCode": 302,
        "location": "/cgi-bin/packetfilter.ha",
    }
    diag = diagnostic_committed("traceroute", "8.8.8.8", 200, "1 hop")
    assert to_json_dict(diag) == {
        "operation": "diagnostic",
        "dryRun": False,
        "committed": True,
        "page": "diag",
        "action": "traceroute",
        "target": "8.8.8.8",
        "guarded": True,
        "dangerous": False,
        "statusCode": 200,
        "result": "1 hop",
    }


def test_quote_arg_prevents_shell_substitution_in_generated_commands():
    assert quote_arg("$(touch marker)") == "'$(touch marker)'"
    assert quote_arg("it's") == "'it'\\''s'"
    assert quote_arg("safe-Value_1.2:3") == "safe-Value_1.2:3"
    assert quote_arg("") == "''"


def test_restore_dry_run_summarizes_planned_and_blocked_steps_with_the_single_confirmation_token():
    result = restore_dry_run(
        [
            _Step(order=1, kind="add-service", page="services", description="a", button="Add", raw_payload={}),
            _Step(order=2, kind="add-forward", page="apphosting", description="b", blocked="x"),
            _Step(order=3, kind="skip", page="packetfilter", description="c"),
        ],
        "RESTORE",
    )
    assert result.operation == "restore"
    assert result.dry_run is True
    assert result.committed is False
    assert result.page == "restore"
    assert result.guarded is True
    assert result.dangerous is False
    assert result.confirmation == "RESTORE"
    assert result.commit_command == "bgwcli restore <dumpfile> --commit --confirm RESTORE"
    assert result.result == "3 steps planned, 1 blocked, 1 skipped"


def test_restore_committed_reports_applied_failed_counts():
    execution = _Execution(
        steps=[
            _StepResult(
                order=1, page="services", kind="add-service", description="a", status="applied", status_code=302
            ),
            _StepResult(order=2, page="dosprotect", kind="form", description="b", status="failed", error="boom"),
            _StepResult(order=3, page="wconfig_unified", kind="form", description="c", status="not-run"),
        ],
        stopped_at=2,
    )
    result = restore_committed(execution)
    assert result.committed is True
    assert result.dry_run is False
    assert result.page == "restore"
    assert result.result == "1 applied, 1 failed, 1 not run; stopped at step 2"
    assert result.status_code == 302


def test_restore_committed_without_stop_uses_last_applied_status_code():
    execution = _Execution(
        steps=[
            _StepResult(
                order=1, page="services", kind="add-service", description="a", status="applied", status_code=302
            ),
            _StepResult(
                order=2, page="services", kind="add-service", description="b", status="applied", status_code=200
            ),
            _StepResult(order=3, page="packetfilter", kind="skip", description="c", status="skipped"),
        ]
    )
    result = restore_committed(execution)
    assert result.result == "2 applied, 0 failed, 0 not run"
    assert result.status_code == 200

    none_applied = restore_committed(_Execution(steps=[]))
    assert none_applied.status_code is None
    assert "statusCode" not in to_json_dict(none_applied)


def test_set_and_submit_results_label_dangerous_pages_truthfully():
    from bgwcli.mutations import MutationPlan

    plan = MutationPlan(page="restart", blocked=False, raw_payload={}, display_payload={}, display_changes={})
    assert set_dry_run(plan, "RESTART").dangerous is True
    assert submit_dry_run(plan, "Restart", "RESTART").dangerous is True
    safe = MutationPlan(page="dosprotect", blocked=False, raw_payload={}, display_payload={}, display_changes={})
    assert set_dry_run(safe, "DOSPROTECT").dangerous is False


def test_set_committed_carries_verification_outcome():
    result = set_committed("dosprotect", 302, "/cgi-bin/dosprotect.ha", verified=False, mismatches={"x": {"wanted": "on", "live": "off"}})
    assert result.verified is False
    assert result.mismatches == {"x": {"wanted": "on", "live": "off"}}
    assert set_committed("dosprotect", 302, None).verified is None
