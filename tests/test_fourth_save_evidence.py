"""Save evidence remains monotonic despite notifications or optional observer faults."""

from dataclasses import replace

import pytest
from test_client import html
from test_review_save_contracts import client_with, form, plan
from test_save_confirmation import SAVED_RED
from test_save_confirmation import clock as confirmation_clock
from test_wifi_no_change import NO_CHANGE

from bgwcli.client import WriteObservation, observe_post, session_pool_full_error
from bgwcli.errors import RouterAuthError, RouterConnectionError
from bgwcli.restore import execute_restore
from bgwcli.save_confirmation import save_notification

clock = confirmation_clock


@pytest.mark.parametrize("text", [
    "No   changes\n detected.\tSave not performed.",
    "No&nbsp;changes detected. Save not&nbsp;performed.",
    "<b>No changes detected</b>. <span>Save not performed</span>.",
])
def test_no_changes_accepts_rendered_notification_whitespace(text):
    assert save_notification("wconfig", f'<div id="error-message-text">{text}</div>').no_changes


@pytest.mark.parametrize("body", [
    '<script id="error-message-text">No changes detected. Save not performed.</script>',
    '<style id="error-message-text">No changes detected. Save not performed.</style>',
    '<p>No changes detected. Save not performed.</p>',
])
def test_no_changes_ignores_nonnotification_text(body):
    assert not save_notification("wconfig", body).no_changes


@pytest.mark.parametrize("matching", [False, True])
def test_later_no_changes_never_erases_earlier_saved_evidence(clock, matching):
    def handle(request, number):
        if number == 1:
            return html(form("wconfig", "old"))
        if number == 2:
            return html(form("wconfig", "old", banner=SAVED_RED))
        return html(form("wconfig", "new" if matching else "old", banner=NO_CHANGE))

    client, wire = client_with(handle)
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == ("applied" if matching else "failed")
    assert result.acknowledgement_observed is True
    assert result.write_performed is not False
    assert result.state_observed is matching
    assert sum(r.method == "POST" and not r.url.endswith("/login.ha") for r in wire.requests) == 1


@pytest.mark.parametrize("fault", ["before", "after", "bad-type", "negative", "bool", "reset"])
@pytest.mark.parametrize("returned", [False, True])
def test_observer_fault_never_replaces_post_result(clock, fault, returned):
    def handle(request, number):
        if number == 1:
            return html(form("etherlan", "old"))
        if not returned:
            raise RouterConnectionError("actual transport failure")
        return html(form("etherlan", "new", banner=SAVED_RED))

    client, wire = client_with(handle)

    class Poster:
        calls = 0

        def observe_writes(self):
            self.calls += 1
            if (fault == "before" and self.calls == 1) or (fault == "after" and self.calls == 2):
                raise RuntimeError("observer failed")
            if fault == "bad-type":
                return object()
            if fault == "negative":
                return WriteObservation(-1, -1)
            if fault == "bool":
                return WriteObservation(True, False)
            if fault == "reset":
                return WriteObservation(5, 5) if self.calls == 1 else WriteObservation(0, 0)
            return client.observe_writes()

        def post_cgi_page(self, page, payload):
            return client.post_cgi_page(page, payload)

    result = execute_restore(Poster(), plan("etherlan")).steps[0]
    assert result.status == ("applied" if returned else "failed")
    assert result.write_attempted is (True if returned else None)
    assert result.write_response_received is (True if returned else None)
    if not returned:
        assert result.error_type == "RouterConnectionError"
        assert "actual transport failure" in result.error
    assert sum(r.method == "POST" and not r.url.endswith("/login.ha") for r in wire.requests) == 1


@pytest.mark.parametrize("cancel", [KeyboardInterrupt, SystemExit])
def test_post_cancellation_is_not_masked_by_observer_failure(cancel):
    class Observer:
        calls = 0

        def observe_writes(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("observer fault")
            return WriteObservation(0, 0)

    with pytest.raises(cancel), observe_post(Observer()):
        raise cancel()


@pytest.mark.parametrize("name", ["ipmask", "dhcp"])
@pytest.mark.parametrize("failure", ["auth", "http", "timeout", "pool", "pending", "login"])
def test_lan_verification_failures_retain_cause_without_inventing_movement(clock, name, failure):
    def handle(request, number):
        if number == 1:
            return html(form("dhcpserver", "old", name))
        if number == 2:
            return html("", status=302, headers={"location": "/cgi-bin/dhcpserver.ha"})
        if failure == "auth":
            raise RouterAuthError("verification authentication failed")
        if failure == "http":
            return html("forbidden operation", status=404)
        if failure == "timeout":
            raise TimeoutError("verification timed out")
        if failure == "pool":
            raise session_pool_full_error(waited_ms=1234, retry_count=3)
        if failure == "login":
            return html('<title>Login</title><input name="nonce" value="abc123">')
        return html(form("dhcpserver", "old", name))

    client, wire = client_with(handle)
    step = replace(plan("dhcpserver", name)[0], reconnect_address="192.0.2.254")
    result = execute_restore(client, [step]).steps[0]
    assert result.status == ("reconnect-required" if failure == "timeout" else "failed")
    assert ("reconnect" in result.error) is (failure == "timeout")
    assert result.write_attempted is True and result.write_response_received is True
    assert result.lan_address_changed is None
    expected = {"auth": "RouterAuthError", "login": "RouterAuthError", "http": "RouterResponseError",
                "timeout": "RouterConnectionError", "pool": "RouterSessionPoolFullError",
                "pending": None}
    assert result.error_type == expected[failure]
    if failure == "pool":
        assert result.session_pool_full and (result.waited_ms, result.retry_count) == (1234, 3)
    assert sum(r.method == "POST" and not r.url.endswith("/login.ha") for r in wire.requests) == 1


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("cancel", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("consumer", ["restore", "rescan"])
def test_observer_cancellation_propagates_without_unbound_cleanup(phase, cancel, consumer):
    from bgwcli.allocation_preflight import (
        AllocationConflict,
        AllocationPreflight,
        rescan_allocation_conflicts,
    )

    client, _ = client_with(lambda request, n: html(form("etherlan", "new", banner=SAVED_RED)))

    class Poster:
        calls = 0

        def observe_writes(self):
            self.calls += 1
            if self.calls == (1 if phase == "before" else 2):
                raise cancel()
            return client.observe_writes()

        def post_cgi_page(self, page, payload):
            return client.post_cgi_page(page, payload)

    with pytest.raises(cancel):
        if consumer == "restore":
            execute_restore(Poster(), plan("etherlan"))
        else:
            preflight = AllocationPreflight(
                [AllocationConflict("192.0.2.1", "02:00:00:00:00:01", "02:00:00:00:00:02", "holder", "off")],
                clear_payload={"Clear": "Clear and Rescan"},
            )
            rescan_allocation_conflicts(Poster(), preflight, log=lambda _: None)


@pytest.mark.parametrize("fault", ["before", "after"])
def test_preflight_observer_failure_preserves_transport_cause(fault):
    from bgwcli.allocation_preflight import (
        AllocationConflict,
        AllocationPreflight,
        AllocationRescanEvidence,
        rescan_allocation_conflicts,
    )

    failure = RouterConnectionError("actual Clear transport failure")

    def handle(request, number):
        if number == 1:
            return html(form("devices", "old"))
        raise failure

    client, wire = client_with(handle)

    class Poster:
        calls = 0

        def observe_writes(self):
            self.calls += 1
            if self.calls == (1 if fault == "before" else 2):
                raise RuntimeError("observer fault")
            return client.observe_writes()

        def post_cgi_page(self, page, payload):
            return client.post_cgi_page(page, payload)

    evidence = AllocationRescanEvidence()
    preflight = AllocationPreflight(
        [AllocationConflict("192.0.2.1", "02:00:00:00:00:01", "02:00:00:00:00:02", "holder", "off")],
        clear_payload={"Clear": "Clear and Rescan"},
    )
    with pytest.raises(RouterConnectionError) as caught:
        rescan_allocation_conflicts(Poster(), preflight, log=lambda _: None, evidence=evidence)
    assert caught.value is failure
    assert evidence.clear_attempted is None and evidence.clear_response_received is None
    assert [r.method for r in wire.requests] == ["GET", "POST"]


def test_autorestore_saved_then_no_changes_keeps_intent_and_stops(clock, tmp_path):
    from bgwcli import cli
    from bgwcli.autorestore import AutorestoreOptions, run_autorestore
    from bgwcli.parser import parse_page
    from bgwcli.recovery_state import RecoveryCheckpoint
    from bgwcli.snapshot import extract_snapshot

    dump = extract_snapshot({"wconfig": parse_page("wconfig", form("wconfig", "new"))}, ts="", router_host="")
    posted = False

    def handle(request, number):
        nonlocal posted
        if request.method == "POST":
            posted = True
            return html(form("wconfig", "old", banner=SAVED_RED))
        return html(form("wconfig", "old", banner=NO_CHANGE if posted else ""))

    client, wire = client_with(handle)
    checkpoint = RecoveryCheckpoint(client.session_identity(), dump, ("wconfig",), root=tmp_path / "recovery")
    result = run_autorestore(
        lambda: (client, False), dump,
        AutorestoreOptions(commit=True, pages=("wconfig",), on_any_diff=True, max_passes=3),
        fetch_pages=cli._fetch_snapshot_pages, checkpoint=checkpoint, sleep=lambda _: None, log=lambda _: None,
    )
    assert result.status == "not-converged" and len(result.passes) == 1
    assert checkpoint.is_active()
    assert result.passes[0]["steps"][0]["acknowledgementObserved"] is True
    assert result.passes[0]["steps"][0]["writePerformed"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1
