import json

import pytest
from test_client import html
from test_review_save_contracts import client_with, form

from bgwcli import cli
from bgwcli import format as fmt
from bgwcli.operations import restore_committed
from bgwcli.restore import RestoreExecution, RestoreStepResult


def unchanged():
    return RestoreStepResult(
        1, "wconfig", "form", "Wi-Fi already desired", "unchanged", status_code=200,
        write_attempted=True, write_response_received=True, write_performed=False,
        acknowledgement_observed=False, state_observed=True,
    )


@pytest.mark.parametrize("status", ["blocked", "not-run"])
def test_no_write_restore_does_not_hide_incomplete_steps(status):
    execution = RestoreExecution([
        unchanged(),
        RestoreStepResult(2, "ipalloc", "allocate", "Required reservation", status,
                          write_attempted=False),
    ])
    result = restore_committed(execution)
    assert result.committed is False and result.write_performed is False
    assert result.outcome == "incomplete"
    assert "1 unchanged" in result.result
    assert ("1 blocked" if status == "blocked" else "1 not run") in result.result


def test_documentary_skip_is_visible_without_claiming_a_write():
    execution = RestoreExecution([
        unchanged(),
        RestoreStepResult(2, "packetfilter", "skip", "Documentary table", "skipped",
                          write_attempted=False),
    ])
    result = restore_committed(execution)
    assert result.outcome == "unchanged" and result.committed is False
    assert "1 skipped" in result.result


def test_unchanged_post_response_keeps_explicit_null_location():
    result = fmt.execution_output(RestoreExecution([unchanged()]))["steps"][0]
    assert "location" in result and result["location"] is None


@pytest.mark.parametrize("page,field,value", [
    ("wconfig", "setting", "new"),
    ("dhcpserver", "ipmask", "255.255.255.0"),
])
@pytest.mark.parametrize("operation", ["set", "submit"])
@pytest.mark.parametrize("status,performed", [("failed", False), ("applied", True)])
def test_wifi_and_lan_operation_mapping_preserves_write_evidence(
    tmp_env, capsys, monkeypatch, page, field, value, operation, status, performed
):
    # A "No changes detected" step is decided by the live re-read: a differing live form is the exit 1 answer.
    live = "old" if status == "failed" else value
    client, _ = client_with(lambda request, number: html(form(page, "old" if number == 1 else live, field)))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    saved = RestoreStepResult(
        1, page, "form", "Save", status, status_code=200,
        error="Save rejected" if status == "failed" else None,
        write_attempted=True, write_response_received=True, write_performed=performed,
        acknowledgement_observed=performed, state_observed=performed,
    )
    monkeypatch.setattr(cli, "execute_restore", lambda *args, **kwargs: RestoreExecution([saved]))
    args = [operation, page]
    if operation == "submit":
        args.append("Save")
    code = cli.main([*args, f"{field}={value}", "--commit", "--confirm", page.upper(), "--json"])
    output = json.loads(capsys.readouterr().out)
    assert output["writePerformed"] is performed
    assert output["writeAttempted"] is True
    assert output["acknowledgementObserved"] is performed
    assert output["committed"] is (status == "applied")
    assert code == (0 if status == "applied" else 1)
