from dataclasses import dataclass, field

import pytest

from bgwcli.format import json_with_nulls
from bgwcli.operations import restore_committed
from bgwcli.restore import RestoreExecution, RestoreStepResult
from bgwcli.types import ParsedField, to_json_dict


def step(status, *, attempted=False, performed=None):
    return RestoreStepResult(
        1, "wconfig", "form", "Save", status,
        write_attempted=attempted, write_performed=performed,
    )


@pytest.mark.parametrize("statuses,outcome", [
    ([], "unchanged"),
    (["blocked"], "incomplete"),
    (["skipped"], "unchanged"),
    (["not-run"], "incomplete"),
    (["blocked", "skipped", "not-run"], "incomplete"),
    (["failed"], "failed"),
])
def test_restore_without_writes_needs_no_synthetic_unchanged_step(statuses, outcome):
    result = restore_committed(RestoreExecution([step(status) for status in statuses]))
    assert result.committed is False
    assert result.write_performed is False
    assert result.outcome == outcome


@pytest.mark.parametrize("status,performed,committed,outcome,aggregate", [
    ("failed", None, False, "failed", None),
    ("failed", False, False, "failed", False),
    ("failed", True, True, "failed", True),
    ("applied", None, True, None, True),
])
def test_restore_distinguishes_unknown_attempt_from_confirmed_write(
    status, performed, committed, outcome, aggregate
):
    result = restore_committed(RestoreExecution([step(status, attempted=True, performed=performed)]))
    assert result.committed is committed
    assert result.write_performed is aggregate
    assert result.outcome == outcome


def test_applied_with_blocked_work_keeps_write_and_incomplete_outcome():
    result = restore_committed(RestoreExecution([
        step("applied", attempted=True), step("blocked"),
    ]))
    assert result.committed is True and result.write_performed is True
    assert result.outcome == "incomplete"
    assert "1 applied" in result.result and "1 blocked" in result.result


@dataclass
class Record:
    name: str = "public"
    location: str | None = None
    private: str | None = field(default="private evidence", metadata={"serialize": False})


@pytest.mark.parametrize("secret", [None, "private evidence"])
def test_null_preservation_never_overrides_private_field_exclusion(secret):
    record = Record(private=secret)
    assert to_json_dict(record) == {"name": "public"}
    assert json_with_nulls(record, {"location", "private"}) == {"name": "public", "location": None}


def test_nested_null_preservation_and_private_control_provenance():
    control = ParsedField("check", "checkbox", "on", True, False, _value_omitted=True)
    value = {"records": (Record(), control)}
    result = json_with_nulls(value, (name for name in ["location"]))
    assert result == {"records": [{"name": "public", "location": None}, to_json_dict(control)]}
