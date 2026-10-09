"""Regressions for LAN redirects, public write evidence, and pool metadata."""

from types import SimpleNamespace

import pytest
from test_client import html
from test_review_save_contracts import client_with, form, plan
from test_save_confirmation import SAVED_RED
from test_save_confirmation import clock as confirmation_clock

from bgwcli import cli, session
from bgwcli.client import RouterResponseError, pool_full_metadata
from bgwcli.errors import RouterAuthError, RouterConnectionError, UsageError
from bgwcli.restore import RestoreStep, execute_restore

clock = confirmation_clock


@pytest.mark.parametrize("name", ["ipmask", "dhcp"])
@pytest.mark.parametrize("read_outcome", ["reachable", "lost", "busy"])
def test_lan_same_address_empty_redirect_reads_before_deciding(clock, name, read_outcome):
    def handle(request, number):
        if number == 1:
            return html(form("dhcpserver", "old", name))
        if number == 2:
            return html("", status=302, headers={"location": "/cgi-bin/dhcpserver.ha"})
        if read_outcome == "lost":
            raise TimeoutError("LAN connection lost")
        if read_outcome == "busy" and number == 3:
            return html("busy", status=503)
        return html(form("dhcpserver", "new", name, SAVED_RED))

    client, wire = client_with(handle)
    result = execute_restore(client, plan("dhcpserver", name)).steps[0]
    assert result.status == ("reconnect-required" if read_outcome == "lost" else "applied")
    assert len(wire.requests) == (4 if read_outcome == "busy" else 3)
    assert [r.method for r in wire.requests[:3]] == ["GET", "POST", "GET"]
    if read_outcome == "lost":
        assert "verify" in result.error
    else:
        assert result.state_observed is True and result.acknowledgement_observed is True
        assert result.lan_address_changed is None


def test_lan_new_address_empty_redirect_never_reads_old_origin(clock):
    client, wire = client_with(lambda request, n: html(
        form("dhcpserver", "old", "ipaddr") if n == 1 else "",
        status=200 if n == 1 else 302,
        headers={} if n == 1 else {"location": "/cgi-bin/dhcpserver.ha"},
    ))
    result = execute_restore(client, plan("dhcpserver", "ipaddr")).steps[0]
    assert result.status == "reconnect-required"
    assert [r.method for r in wire.requests] == ["GET", "POST"]


@pytest.mark.parametrize("name", ["ipmask", "dhcp"])
def test_lan_same_address_redirect_preserves_cli_closing_diff(clock, tmp_env, tmp_path, capsys, monkeypatch, name):
    import json

    from bgwcli.dumpfile import write_dump_file
    from bgwcli.parser import parse_page
    from bgwcli.snapshot import extract_snapshot

    page = "dhcpserver"
    requested = form(page, "new", name, SAVED_RED)
    dump = extract_snapshot({page: parse_page(page, requested)}, ts="", router_host="", include=(page,))
    path = tmp_path / "lan.json"
    write_dump_file(path, dump)

    def handle(request, number):
        if number <= 2:
            return html(form(page, "old", name))
        if number == 3:
            return html("", status=302, headers={"location": "/cgi-bin/dhcpserver.ha"})
        return html(requested)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(path), "--include", page, "--commit", "--confirm", "RESTORE", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["diff"] is not None
    assert output["execution"]["steps"][0]["status"] == "applied"
    assert [r.method for r in wire.requests] == ["GET", "GET", "POST", "GET", "GET"]


def test_legacy_poster_exception_keeps_unknown_delivery():
    class Poster:
        def post_cgi_page(self, page, fields):
            raise RouterConnectionError("transport unavailable")

    result = execute_restore(Poster(), [RestoreStep(1, "form", "etherlan", "Save", raw_payload={})]).steps[0]
    assert result.write_attempted is None
    assert "sent once" not in result.error and "attempted once" not in result.error
    assert "unknown" in result.error and "verify" in result.error


def test_autorestore_does_not_retry_legacy_unknown_delivery():
    from test_autorestore import Fetcher, make_dump, reset_pages

    from bgwcli.autorestore import AutorestoreOptions, run_autorestore

    class Poster:
        calls = 0

        def post_cgi_page(self, page, fields):
            self.calls += 1
            raise RouterConnectionError("delivery unobserved")

    client = Poster()
    sleeps = []
    result = run_autorestore(
        lambda: (client, False), make_dump(),
        AutorestoreOptions(commit=True, max_passes=3, pages=("dosprotect",)),
        fetch_pages=Fetcher(reset_pages()), sleep=sleeps.append, log=lambda _: None,
    )
    assert result.status == "error" and result.write_unanswered and len(result.passes) == 1
    assert client.calls == 1 and sleeps == []
    assert "unknown" in result.reason and "the next timer run will re-read the gateway and retry" in result.reason


@pytest.mark.parametrize("phase", ["nonce", "authentication", "transport", "http-error"])
def test_public_observer_distinguishes_presend_transport_and_response(phase):
    def handle(request, number):
        if number == 1:
            if phase == "nonce":
                raise TimeoutError("nonce unavailable")
            if phase == "authentication":
                raise RouterAuthError("session unavailable")
            return html(form("etherlan", "old"))
        if phase == "transport":
            raise TimeoutError("reply lost")
        return html("rejected", status=500)

    client, _ = client_with(handle)
    before = client.observe_writes()
    result = execute_restore(client, [RestoreStep(1, "form", "etherlan", "Save", raw_payload={})]).steps[0]
    after = client.observe_writes()
    attempted = phase not in {"nonce", "authentication"}
    assert after.attempts - before.attempts == attempted
    assert after.responses - before.responses == (phase == "http-error")
    assert result.write_attempted is attempted
    assert result.write_response_received is (phase == "http-error")
    assert ("sent once" in result.error) is (phase == "http-error")


@pytest.mark.parametrize("shape", ["camel-map", "snake-map", "camel-object", "snake-object", "error"])
def test_pool_metadata_normalization_preserves_shapes(shape):
    data = {"sessionPoolFull": True, "waitedMs": 4200, "retryCount": 7}
    if shape.startswith("snake") or shape == "error":
        data = {"session_pool_full": True, "waited_ms": 4200, "retry_count": 7}
    value = data if shape.endswith("map") else SimpleNamespace(**data)
    if shape == "error":
        value = RouterConnectionError("example")
        value.__dict__.update(data)
    assert pool_full_metadata(value) == (4200, 7)
    assert session._result_pool_full_metadata(value) == (4200, 7)


def test_pool_metadata_aggregation_retains_one_observed_pair():
    values = [{"sessionPoolFull": True, "waitedMs": 9000, "retryCount": 2},
              {"session_pool_full": True, "waited_ms": 5000, "retry_count": 12}]
    assert session._result_pool_full_metadata(values) == (9000, 2)


@pytest.mark.parametrize("value", [None, {}, SimpleNamespace(), {"waitedMs": None, "retryCount": None}])
def test_pool_metadata_absent_values_default_to_zero(value):
    assert pool_full_metadata(value) == (0, 0)


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("outcome", ["presend", "transport", "rejected"])
def test_preflight_uses_public_observer_or_unknown_legacy_evidence(observed, outcome):
    from bgwcli.allocation_preflight import (
        AllocationConflict,
        AllocationPreflight,
        AllocationRescanEvidence,
        rescan_allocation_conflicts,
    )

    def handle(request, number):
        if number == 1:
            if outcome == "presend":
                raise TimeoutError("nonce unavailable")
            return html(form("devices", "old"))
        if outcome == "transport":
            raise TimeoutError("reply lost")
        return html("rejected", status=500)

    client, _ = client_with(handle)

    class Legacy:
        def post_cgi_page(self, page, fields):
            return client.post_cgi_page(page, fields)

    class PublicOnly(Legacy):
        def observe_writes(self):
            return client.observe_writes()

    preflight = AllocationPreflight(
        [AllocationConflict("192.0.2.1", "02:00:00:00:00:01", "02:00:00:00:00:02", "holder", "off")],
        clear_payload={"Clear": "Clear and Rescan"},
    )
    evidence = AllocationRescanEvidence()
    with pytest.raises(RouterResponseError if outcome == "rejected" else RouterConnectionError):
        rescan_allocation_conflicts(PublicOnly() if observed else Legacy(), preflight, log=lambda _: None,
                                    evidence=evidence)
    assert evidence.clear_attempted is ((outcome != "presend") if observed else None)
    assert evidence.clear_response_received is ((outcome == "rejected") if observed else None)


def test_legacy_returned_response_establishes_write_and_response_evidence():
    from test_save_confirmation import response

    from bgwcli.allocation_preflight import (
        AllocationConflict,
        AllocationPreflight,
        AllocationRescanEvidence,
        rescan_allocation_conflicts,
    )

    class Poster:
        def post_cgi_page(self, page, fields):
            return response(page, "rejected", status=500)

    result = execute_restore(Poster(), [RestoreStep(1, "form", "etherlan", "Save", raw_payload={})]).steps[0]
    assert result.write_attempted is True and result.write_response_received is True
    assert "sent once" in result.error
    preflight = AllocationPreflight(
        [AllocationConflict("192.0.2.1", "02:00:00:00:00:01", "02:00:00:00:00:02", "holder", "off")],
        clear_payload={"Clear": "Clear and Rescan"},
    )
    evidence = AllocationRescanEvidence()
    with pytest.raises(UsageError, match="HTTP 500"):
        rescan_allocation_conflicts(Poster(), preflight, log=lambda _: None, evidence=evidence)
    assert evidence.clear_attempted is True and evidence.clear_response_received is True
