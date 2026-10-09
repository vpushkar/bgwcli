"""Malformed save readback retains a safe diagnosis and independent write evidence."""

import pytest
from test_client import html
from test_review_save_contracts import client_with, plan
from test_save_confirmation import SAVED_RED
from test_save_confirmation import clock as confirmation_clock
from test_wifi_no_change import NO_CHANGE

from bgwcli import restore
from bgwcli.errors import SnapshotExtractionError
from bgwcli.restore import RestorePostcondition, RestoreStep, confirm_saved_page, execute_restore
from bgwcli.snapshot import SnapshotService

clock = confirmation_clock
UNREADABLE_VALUE = "synthetic-private-value"
MAC = "02:00:00:00:00:01"


def service_table(port):
    return (
        '<table><tr><th>Service Name</th><th>Global Port Range</th>'
        '<th>Base Host Port</th><th>Protocol</th></tr>'
        f'<tr><td>Example</td><td>{port}</td><td>42</td><td>TCP</td></tr></table>'
    )


def malformed_table(page):
    if page == "services":
        return service_table(UNREADABLE_VALUE)
    return (
        '<table><tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Allocation</th></tr>'
        f'<tr><td>192.0.2.1</td><td>{UNREADABLE_VALUE}</td><td>Fixed Allocation</td></tr></table>'
    )


def service_step():
    return RestoreStep(
        1, "add-service", "services", "add synthetic service",
        raw_payload={"Service": "Example", "Add": "Add"},
        postcondition=RestorePostcondition(service=SnapshotService("Example", 42, 42, 42, "TCP")),
    )


def scripted_client(page, replies, post_body=""):
    def handle(request, number):
        if number == 1:
            assert request.method == "GET"
            return html('<input name="nonce" value="abc123"><input type="submit" name="Save" value="Save">')
        if number == 2:
            assert request.method == "POST"
            return html(post_body)
        assert request.method == "GET", "save verification must never repeat the POST"
        reply = replies[min(number - 3, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return html(reply)

    return client_with(handle)


@pytest.mark.parametrize("page", ["services", "ipalloc"])
@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("in_post", [False, True])
def test_unreadable_save_state_retains_acknowledgement_and_safe_diagnosis(clock, page, saved, in_post):
    body = (SAVED_RED if saved else "") + malformed_table(page)
    client, wire = scripted_client(page, [body], post_body=body if in_post else "")
    if page == "services":
        result = execute_restore(client, [service_step()]).steps[0]
    else:
        payload = {f"alloc_{MAC}": "192.0.2.1", "Save": "Save"}
        result = confirm_saved_page(client, page, client.post_cgi_page(page, payload), payload=payload)

    # An acknowledged save whose state never became readable keeps the structural exception type.
    assert result.status == "failed" and result.error_type == ("SnapshotExtractionError" if saved else None)
    assert result.state_observed is None and result.acknowledgement_observed is saved
    assert result.write_performed is (True if saved else None)
    assert result.write_attempted is True and result.write_response_received is True
    assert "requested state could not be read" in result.error
    assert ("Changes saved was observed" if saved else "Changes saved was not observed") in result.error
    assert UNREADABLE_VALUE not in result.error
    assert "sent once" in result.error and "verify" in result.error
    assert sum(request.method == "POST" for request in wire.requests) == 1
    assert clock.now == 3.0


@pytest.mark.parametrize("matching", [False, True])
def test_valid_state_after_extraction_failure_clears_unreadable_diagnosis(clock, matching):
    client, wire = scripted_client("services", [
        SAVED_RED + malformed_table("services"), service_table(42 if matching else 80),
    ])
    result = execute_restore(client, [service_step()]).steps[0]
    assert result.status == ("applied" if matching else "failed")
    assert result.state_observed is matching and result.acknowledgement_observed is True
    assert result.write_performed is True and result.error_type is None
    if not matching:
        assert "state is not visible" in result.error
        assert "could not be read" not in result.error
    assert sum(request.method == "POST" for request in wire.requests) == 1


def test_unreadable_state_after_transport_recovery_has_its_own_diagnosis(clock):
    client, wire = scripted_client("services", [
        TimeoutError("synthetic transport loss"), SAVED_RED + malformed_table("services"),
    ])
    result = execute_restore(client, [service_step()]).steps[0]
    assert result.status == "failed" and result.error_type == "SnapshotExtractionError"
    assert result.state_observed is None and result.acknowledgement_observed is True
    assert "requested state could not be read" in result.error
    assert "Timed out connecting" not in result.error
    assert sum(request.method == "POST" for request in wire.requests) == 1


def test_transport_failure_after_unreadable_state_retains_actual_cause(clock):
    client, wire = scripted_client("services", [
        SAVED_RED + malformed_table("services"), TimeoutError("synthetic transport loss"),
    ])
    result = execute_restore(client, [service_step()]).steps[0]
    assert result.status == "failed" and result.error_type == "RouterConnectionError"
    assert result.state_observed is None and result.acknowledgement_observed is True
    assert result.write_performed is True
    assert "verification read failed" in result.error
    assert "Timed out connecting to http://router.local/cgi-bin/services.ha" in result.error
    assert "could not be read" not in result.error
    assert sum(request.method == "POST" for request in wire.requests) == 1


def test_no_change_with_unreadable_state_retains_explicit_nonwrite(clock, monkeypatch):
    # Form extraction has no malformed-table path; inject its documented exception at the boundary.
    def unreadable(step, body):
        raise SnapshotExtractionError(UNREADABLE_VALUE)

    monkeypatch.setattr(restore, "_postcondition_matches", unreadable)
    client, wire = scripted_client("wconfig", [], post_body=NO_CHANGE)
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == "failed" and result.error_type is None
    assert result.state_observed is None and result.acknowledgement_observed is False
    assert result.write_performed is False
    assert "No changes detected. Save not performed." in result.error
    assert "requested state could not be verified" in result.error
    assert UNREADABLE_VALUE not in result.error
    assert [request.method for request in wire.requests] == ["GET", "POST"]
    assert not clock.sleeps
