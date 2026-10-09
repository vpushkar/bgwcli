"""Save outcomes distinguish evidence deadlines from actual endpoint failures."""

import json

import pytest
from test_client import html
from test_review_save_contracts import client_with, form, plan
from test_save_confirmation import ERROR, SAVED_RED
from test_save_confirmation import clock as confirmation_clock
from test_wifi_no_change import NO_CHANGE

from bgwcli import cli
from bgwcli.client import session_pool_full_error
from bgwcli.errors import RouterAuthError
from bgwcli.restore import execute_restore

clock = confirmation_clock


@pytest.mark.parametrize("page,name", [("wconfig", "setting"), ("dhcpserver", "ipmask"), ("dhcpserver", "dhcp")])
@pytest.mark.parametrize("post_status", [200, 302])
@pytest.mark.parametrize("scenario,status,error_type,ack,state", [
    ("missing-ack", "failed", None, False, True),
    ("wrong-state", "failed", None, True, False),
    ("saved", "applied", None, True, True),
    ("rejected", "failed", None, False, None),
    ("connection", "failed", "RouterConnectionError", False, None),
    ("http", "failed", "RouterResponseError", False, None),
    ("pool", "failed", "RouterSessionPoolFullError", False, None),
    ("auth", "failed", "RouterAuthError", False, None),
])
def test_save_failure_path_matrix(clock, page, name, post_status, scenario, status, error_type, ack, state):
    def handle(request, number):
        if number == 1:
            return html(form(page, "old", name))
        if number == 2:
            return html("", status=post_status, headers={"location": f"/cgi-bin/{page}.ha"})
        if scenario == "connection":
            raise TimeoutError("actual verification transport loss")
        if scenario == "http":
            return html("forbidden operation", status=404)
        if scenario == "pool":
            raise session_pool_full_error(waited_ms=1234, retry_count=3)
        if scenario == "auth":
            raise RouterAuthError("actual authentication failure")
        if scenario == "rejected":
            return html(ERROR)
        return html(form(page, "old" if scenario == "wrong-state" else "new", name,
                         "" if scenario == "missing-ack" else SAVED_RED))

    client, wire = client_with(handle)
    result = execute_restore(client, plan(page, name)).steps[0]
    expected_status = "reconnect-required" if page == "dhcpserver" and scenario == "connection" else status
    assert result.status == expected_status
    assert result.error_type == error_type
    assert result.acknowledgement_observed is ack and result.state_observed is state
    assert result.write_attempted is True and result.write_response_received is True
    assert sum(request.method == "POST" for request in wire.requests) == 1
    assert len(wire.requests) >= 3, "mask/DHCP redirects must read the reachable original endpoint"
    if scenario == "pool":
        assert result.session_pool_full is True
        assert (result.waited_ms, result.retry_count) == (1234, 3)
    if scenario in {"missing-ack", "wrong-state"}:
        assert "Timed out" in result.error and "verify" in result.error
        assert "reconnect" not in result.error
        assert clock.now == 3.0


@pytest.mark.parametrize("operation", ["set", "submit"])
@pytest.mark.parametrize("page", ["wconfig", "wconfig_unified"])
def test_wifi_cli_missing_ack_emits_structured_write_evidence(clock, tmp_env, capsys, monkeypatch, operation, page):
    client, wire = client_with(lambda request, number: html(form(page, "old" if number <= 2 else "new")))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    args = [operation, page, *(["Save"] if operation == "submit" else []), "setting=new",
            "--commit", "--confirm", page.upper().replace("_", "-"), "--json"]
    code = cli.main(args)
    captured = capsys.readouterr()
    assert captured.out, captured.err
    output = json.loads(captured.out)
    assert code == 2 and output["outcome"] == "failed"
    assert output["committed"] is False and output["writeAttempted"] is True
    assert output["acknowledgementObserved"] is False
    assert "Timed out" in output["warning"] and "verify" in output["warning"]
    assert sum(request.method == "POST" for request in wire.requests) == 1


@pytest.mark.parametrize("matching", [False, True])
def test_combined_saved_and_no_save_notification_keeps_write_evidence(clock, matching):
    client, wire = client_with(lambda request, number: html(
        form("wconfig", "old") if number == 1 else
        form("wconfig", "new" if matching else "old", banner=SAVED_RED + NO_CHANGE)
    ))
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == ("applied" if matching else "failed")
    assert result.acknowledgement_observed is True and result.write_performed is True
    assert result.state_observed is matching and result.error_type is None
    assert sum(request.method == "POST" for request in wire.requests) == 1


def test_reachable_reply_after_transport_failure_clears_connection_diagnosis(clock):
    def handle(request, number):
        if number <= 2:
            return html(form("wconfig", "old"))
        if number == 3:
            raise TimeoutError("transient read loss")
        return html(form("wconfig", "new"))

    client, wire = client_with(handle)
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == "failed" and result.error_type is None
    assert result.state_observed is True and result.acknowledgement_observed is False
    assert sum(request.method == "POST" for request in wire.requests) == 1


@pytest.mark.parametrize("notification", [NO_CHANGE, SAVED_RED])
@pytest.mark.parametrize("rejection_first", [False, True])
def test_actual_rejection_wins_over_other_notifications(clock, notification, rejection_first):
    banner = ERROR + notification if rejection_first else notification + ERROR
    client, wire = client_with(lambda request, number: html(
        form("wconfig", "old") if number == 1 else form("wconfig", "new", banner=banner)
    ))
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == "failed" and "router rejected" in result.error
    assert sum(request.method == "POST" for request in wire.requests) == 1
    assert not clock.sleeps
