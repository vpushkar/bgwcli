"""Restore orchestration gates writes on allocation conflict preflight and a fresh snapshot.

Only HTTP and the conflict inspector/rescanner boundary are replaced. Snapshot extraction,
diffing, plan building, mutation execution, and save acknowledgement use production code.
The allocation_preflight module's own tests cover its device parsing and timed rescans.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from integration_html import CHANGES_SAVED_HTML, allocation_saved_html, entry_page_html
from page_builders import (
    apphosting_page,
    dosprotect_page,
    hidden,
    ipalloc_page,
    page,
    select,
    services_page,
    sysinfo_page,
)
from test_allocation_preflight import devices_html, ipalloc_html

from bgwcli import allocation_preflight, autorestore, cli
from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.config import GlobalOptions
from bgwcli.errors import UsageError
from bgwcli.snapshot import SnapshotReservation, extract_snapshot
from bgwcli.types import HttpResponse

TARGET_MAC = "02:0a:0b:0c:0d:04"
OTHER_MAC = "02:0a:0b:0c:0d:09"
TARGET_IP = "192.168.1.67"
SSH_ROW = ("ssh", "2222-2222", "22", "TCP")
STALE_ROW = ("stale", "8000-8000", "8000", "TCP")


def pages(*, restored=False, fresh=False, reset=False, with_stale=False):
    services = [] if reset else [SSH_ROW]
    if with_stale:
        services.append(STALE_ROW)
    hosting = apphosting_page(
        rows=[("ssh", "target")] if restored else [],
        service_options=["ssh"],
        device_options=[(TARGET_MAC, "target")],
    )
    hosting = replace(
        hosting, fields=[*hosting.fields, hidden("deviceinfo", "fresh-device-info" if fresh else "old-device-info")]
    )
    allocation = (
        [(TARGET_IP, TARGET_MAC, "on", "Fixed Allocation")]
        if restored
        else [(TARGET_IP if fresh else "192.168.1.80", TARGET_MAC, "on", "DHCP Allocation")]
    )
    if not fresh and not restored:
        allocation.append((TARGET_IP, OTHER_MAC, "off", "DHCP Allocation"))
    return {
        "sysinfo": sysinfo_page(),
        "services": services_page(
            rows=services, remove_buttons=["Remove_4", "Remove_9"] if fresh and with_stale else None
        ),
        "apphosting": hosting,
        "ipalloc": ipalloc_page(rows=allocation),
        "packetfilter": page("packetfilter"),
        "dosprotect": dosprotect_page(
            selects=[select("flood_protect", [("on", "On", restored), ("off", "Off", not restored)])]
        ),
        "wconfig": page("wconfig"),
        "wconfig_unified": page("wconfig_unified"),
    }


def baseline():
    return extract_snapshot(pages(restored=True), ts="synthetic", router_host="synthetic.invalid")


def response(page_id, body=CHANGES_SAVED_HTML):
    return HttpResponse(200, "OK", {}, body, f"https://synthetic.invalid/cgi-bin/{page_id}.ha")


class Router:
    def __init__(self, *, holder_mac=OTHER_MAC, persistent=False):
        self.holder_mac = holder_mac
        self.persistent = persistent
        self.events = []
        self.posts = []
        self.gets = []

    def get_cgi_page(self, page_id, **kwargs):
        self.gets.append(page_id)
        self.events.append(f"get:{page_id}")
        if page_id == "devices":
            return response(page_id, f"<p>{self.holder_mac} holds {TARGET_IP}</p>")
        if page_id == "ipalloc":
            return response(page_id, entry_page_html(TARGET_MAC, [TARGET_IP]))
        raise AssertionError(f"unexpected HTTP GET {page_id}")

    def post_cgi_page(self, page_id, fields):
        fields = dict(fields)
        self.posts.append((page_id, fields))
        self.events.append("clear" if page_id == "devices" else f"post:{page_id}")
        if page_id == "ipalloc" and "Save" in fields:
            return response(page_id, allocation_saved_html(TARGET_MAC, TARGET_IP))
        if page_id not in {"devices", "ipalloc"}:
            from integration_html import saved_configuration_html
            return response(page_id, saved_configuration_html(page_id, fields))
        return response(page_id)


class Fetcher:
    def __init__(self, *schedule):
        self.schedule = schedule
        self.calls = 0

    def __call__(self, client, page_ids):
        client.events.append(f"snapshot:{self.calls}")
        current = self.schedule[min(self.calls, len(self.schedule) - 1)]
        self.calls += 1
        return {key: value for key, value in current.items() if key in page_ids}, []


@pytest.fixture
def orchestration(monkeypatch):
    """Controlled preflight boundary; its observable Clear still uses the fake HTTP client."""
    inspected = []

    def inspect(client, requests):
        inspected.append(tuple(requests))
        client.get_cgi_page("devices")
        conflicts = [
            AllocationConflict(request.ip, request.mac, client.holder_mac, "other-device", "off")
            for request in requests
            if request.ip == TARGET_IP and client.holder_mac != request.mac
        ]
        return AllocationPreflight(conflicts, {"Clear": "Clear Device List"} if conflicts else None)

    def rescan(client, preflight, *, log=lambda _line: None, evidence=None):
        assert preflight.conflicts
        client.post_cgi_page("devices", preflight.clear_payload)
        if evidence is not None:
            evidence.clear_attempted = True
            evidence.clear_response_received = True
            evidence.clear_accepted = True
        # This represents the blocking module call; the module's own fake clock tests enforce duration.
        client.events.extend(["minimum-rescan-wait", "rescan-finished"])
        if client.persistent:
            raise UsageError(f"allocation conflict persists: {TARGET_IP} is held by {OTHER_MAC}")
        client.holder_mac = TARGET_MAC
        if evidence is not None:
            evidence.ownership_verified = True

    for module in (cli, autorestore):
        monkeypatch.setattr(module, "inspect_allocation_conflicts", inspect, raising=False)
        monkeypatch.setattr(module, "rescan_allocation_conflicts", rescan, raising=False)
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    return inspected


def run_restore(monkeypatch, client, fetcher, *, commit=True, include=None, prune=False):
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", fetcher)
    command = cli.Command(
        "restore",
        ["synthetic-dump.json"],
        GlobalOptions(json=True),
        commit=commit,
        confirm="RESTORE" if commit else None,
        include=include,
        prune=prune,
    )
    cli._run_restore(client, command)
    return command


def run_auto(client, fetcher, *, commit=True, selected=None):
    return run_autorestore(
        lambda: (client, False),
        baseline(),
        AutorestoreOptions(commit=commit, max_passes=1, wait_seconds=0, pages=selected),
        fetch_pages=fetcher,
        log=lambda _line: None,
        sleep=lambda _seconds: pytest.fail("a one-pass restore must not sleep between passes"),
    )


def assert_preflight_order(client):
    assert client.events.count("clear") == 1
    clear = client.events.index("clear")
    wait = client.events.index("minimum-rescan-wait")
    refreshed = client.events.index("snapshot:1")
    config_posts = [index for index, event in enumerate(client.events) if event.startswith("post:")]
    assert config_posts
    assert clear < wait < refreshed < min(config_posts)


def test_restore_dry_run_reports_conflicts_without_clear_or_configuration_posts(orchestration, monkeypatch, capsys):
    client = Router()
    fetcher = Fetcher(pages())
    run_restore(monkeypatch, client, fetcher, commit=False)
    output = json.loads(capsys.readouterr().out)
    assert output["allocationPreflight"]["rescanPlanned"] is True
    assert OTHER_MAC in json.dumps(output["allocationPreflight"])
    assert output["steps"] == []  # Full plan must wait for refreshed identities and row indices.
    assert client.posts == []
    assert fetcher.calls == 1
    assert orchestration == [(SnapshotReservation(TARGET_MAC, TARGET_IP),)]


def test_restore_excluding_ipalloc_never_reads_devices_or_clears(orchestration, monkeypatch, capsys):
    client = Router()
    run_restore(monkeypatch, client, Fetcher(pages(), pages(restored=True)), include=["dosprotect"])
    capsys.readouterr()
    assert orchestration == []
    assert "devices" not in client.gets
    assert [page_id for page_id, _ in client.posts] == ["dosprotect"]


def test_restore_same_mac_holding_the_requested_ip_never_clears(orchestration, monkeypatch, capsys):
    client = Router(holder_mac=TARGET_MAC)
    command = run_restore(monkeypatch, client, Fetcher(pages(), pages(restored=True)))
    capsys.readouterr()
    assert command.exit_code == 0
    assert all(page_id != "devices" for page_id, _ in client.posts)
    assert any(page_id == "ipalloc" and "Save" in fields for page_id, fields in client.posts)


def test_restore_rescans_and_rebuilds_the_plan_before_nat_and_configuration_posts(orchestration, monkeypatch, capsys):
    client = Router()
    fetcher = Fetcher(pages(with_stale=True), pages(fresh=True, with_stale=True), pages(restored=True))
    command = run_restore(monkeypatch, client, fetcher, prune=True)
    output = json.loads(capsys.readouterr().out)
    assert command.exit_code == 0
    assert_preflight_order(client)
    assert output["allocationPreflight"]["rescanPerformed"] is True
    removals = [
        fields
        for page_id, fields in client.posts
        if page_id == "services" and any(key.startswith("Remove_") for key in fields)
    ]
    assert [{key: value for key, value in fields.items() if key.startswith("Remove_")} for fields in removals] == [
        {"Remove_9": "Remove"}
    ]
    nat_add = next(fields for page_id, fields in client.posts if page_id == "apphosting" and "Add" in fields)
    assert nat_add["deviceinfo"] == "fresh-device-info"
    assert fetcher.calls == 3


def test_restore_persistent_holder_stops_after_one_clear_without_configuration_write(
    orchestration, monkeypatch, capsys
):
    client = Router(persistent=True)
    fetcher = Fetcher(pages())
    command = run_restore(monkeypatch, client, fetcher)
    output = json.loads(capsys.readouterr().out)
    assert command.exit_code == 2
    assert output["allocationPreflight"]["planStatus"] == "blocked"
    assert "conflict persists" in output["allocationPreflight"]["reason"]
    assert client.posts == [("devices", {"Clear": "Clear Device List"})]
    assert fetcher.calls == 1


def test_autorestore_ordinary_drift_never_inspects_or_clears(orchestration):
    client = Router()
    result = run_auto(client, Fetcher(pages()))  # The dumped service survives: partial loss is drift.
    assert result.status == "no-reset"
    assert not result.detected
    assert orchestration == []
    assert client.posts == []
    assert "devices" not in client.gets


def test_autorestore_detected_reset_dry_run_inspects_but_never_clears(orchestration):
    client = Router()
    result = run_auto(client, Fetcher(pages(reset=True)), commit=False)
    assert result.status == "restore-needed"
    assert result.detected
    assert orchestration
    assert client.gets == ["devices"]
    assert client.posts == []
    assert result.plan is None


def test_autorestore_detected_reset_rescans_then_refreshes_before_restore(orchestration):
    client = Router()
    fetcher = Fetcher(pages(reset=True), pages(reset=True, fresh=True), pages(restored=True))
    result = run_auto(client, fetcher)
    assert result.status == "converged"
    assert result.detected
    assert_preflight_order(client)
    assert fetcher.calls == 3
    assert any(page_id == "services" and "Add" in fields for page_id, fields in client.posts)


def test_autorestore_excluding_ipalloc_does_not_inspect_devices_or_clear(orchestration):
    client = Router()
    result = run_auto(client, Fetcher(pages(), pages(restored=True)), selected=("dosprotect",))
    assert result.status == "converged"
    assert orchestration == []
    assert "devices" not in client.gets
    assert [page_id for page_id, _ in client.posts] == ["dosprotect"]


def test_autorestore_persistent_holder_returns_error_before_other_configuration_posts(orchestration):
    client = Router(persistent=True)
    fetcher = Fetcher(pages(reset=True))
    result = run_auto(client, fetcher)
    assert result.status == "error"
    assert result.exit_code == 2
    assert "conflict persists" in result.reason
    assert client.posts == [("devices", {"Clear": "Clear Device List"})]
    assert fetcher.calls == 1


class SplitOwnershipRouter(Router):
    """The two gateway pages disagree; only HTTP responses are substituted."""

    def __init__(self, *, allocation_only=False):
        super().__init__()
        self.allocation_only = allocation_only

    def get_cgi_page(self, page_id, **kwargs):
        self.gets.append(page_id)
        self.events.append(f"get:{page_id}")
        if page_id == "devices":
            ip = "192.168.1.140" if self.allocation_only else TARGET_IP
            return response(page_id, devices_html([(ip, OTHER_MAC, "holder", "on")]))
        if page_id == "ipalloc":
            rows = [(TARGET_IP, OTHER_MAC, "holder", "on")] if self.allocation_only else []
            return response(page_id, ipalloc_html(rows).replace("DHCP Allocation", "Fixed Allocation"))
        raise AssertionError(f"unexpected HTTP GET {page_id}")


@pytest.fixture
def fast_rescan_clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(allocation_preflight, "monotonic", lambda: now[0])
    monkeypatch.setattr(allocation_preflight, "sleep", sleep)
    monkeypatch.setattr(allocation_preflight, "RESCAN_SETTLE_SECONDS", 1.0)
    monkeypatch.setattr(allocation_preflight, "RESCAN_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(allocation_preflight, "RESCAN_POLL_SECONDS", 1.0)


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_real_preflight_never_writes_config_while_devices_retains_holder(
    fast_rescan_clock, monkeypatch, capsys, automatic
):
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    client = SplitOwnershipRouter()
    fetcher = Fetcher(pages(reset=True), pages(reset=True, fresh=True), pages(restored=True))

    if automatic:
        result = run_auto(client, fetcher)
        assert result.status == "error"
        assert OTHER_MAC in result.reason
    else:
        command = run_restore(monkeypatch, client, fetcher)
        output = json.loads(capsys.readouterr().out)
        assert command.exit_code == 2
        assert OTHER_MAC in output["allocationPreflight"]["reason"]

    assert client.posts == [("devices", {"Clear": "Clear & Rescan"})]
    assert fetcher.calls == 1  # Never proceeded to fresh planning or any config mutation.


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_real_preflight_dry_run_reports_fixed_holder_missing_from_devices_ip(monkeypatch, capsys, automatic):
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    client = SplitOwnershipRouter(allocation_only=True)
    fetcher = Fetcher(pages(reset=True))

    if automatic:
        result = run_auto(client, fetcher, commit=False)
        assert result.status == "error"
        report = result.allocation_preflight
    else:
        run_restore(monkeypatch, client, fetcher, commit=False)
        report = json.loads(capsys.readouterr().out).get("allocationPreflight")

    assert report is not None
    assert OTHER_MAC in json.dumps(report)
    assert report["rescanPlanned"] is False
    assert report["planStatus"] == "blocked"
    assert report["planComplete"] is False
    assert "manual" in report["reason"]
    assert client.posts == []


@pytest.mark.parametrize("persist_fails", [False, True])
def test_autorestore_persists_intent_before_required_clear(orchestration, tmp_path, persist_fails):
    from bgwcli.recovery_state import RecoveryCheckpoint

    store = RecoveryCheckpoint("synthetic.invalid", baseline(), root=tmp_path / "recovery")
    if persist_fails:
        store.path.parent.write_text("not a directory")

    class CheckpointRouter(Router):
        def post_cgi_page(self, page_id, fields):
            assert store.is_active(), "every write including Clear requires published recovery intent"
            return super().post_cgi_page(page_id, fields)

    router = CheckpointRouter()
    result = run_autorestore(
        lambda: (router, False),
        baseline(),
        AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(pages(reset=True), pages(fresh=True), pages(restored=True)),
        checkpoint=store,
        log=lambda _: None,
    )
    if persist_fails:
        assert result.status == "error" and not router.posts
    else:
        assert result.status == "converged" and router.posts[0][0] == "devices"
        assert not store.is_active()


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_pending_dry_run_reports_incomplete_plan_instead_of_zero_completed_steps(
    orchestration, monkeypatch, capsys, automatic
):
    client = Router()
    if automatic:
        output = autorestore.result_output(run_auto(client, Fetcher(pages(reset=True)), commit=False))
    else:
        run_restore(monkeypatch, client, Fetcher(pages()), commit=False)
        output = json.loads(capsys.readouterr().out)
    assert output["allocationPreflight"]["planStatus"] == "pending"
    assert output["planComplete"] is False
    assert output["planStatus"] == "pending"
    assert "0 steps planned" not in json.dumps(output)
    assert client.posts == []


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_read_only_unverifiable_ownership_reports_blocked_reason(monkeypatch, capsys, automatic):
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    client = Router()  # Deliberately has no recognizable Devices ownership table.
    if automatic:
        output = autorestore.result_output(run_auto(client, Fetcher(pages(reset=True)), commit=False))
    else:
        run_restore(monkeypatch, client, Fetcher(pages()), commit=False)
        output = json.loads(capsys.readouterr().out)
    report = output["allocationPreflight"]
    assert report["planStatus"] == "blocked"
    assert report["planComplete"] is False
    assert "recognizable" in report["reason"]
    assert report["error"]["type"] == "UsageError"
    assert client.posts == []


def test_commit_unverifiable_ownership_reports_blocked_preflight_instead_of_raising(monkeypatch, capsys):
    """--commit must keep the dry-run's structured preflight failure: exit 2 with planStatus blocked,
    not a bare UsageError (exit 1) that a wrapper reads as 'the restore drifted'."""
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    client = Router()  # Deliberately has no recognizable Devices ownership table.
    command = run_restore(monkeypatch, client, Fetcher(pages()), commit=True)
    output = json.loads(capsys.readouterr().out)
    assert command.exit_code == 2
    assert output["ok"] is False
    assert output["planStatus"] == "blocked"
    assert output["planComplete"] is False
    report = output["allocationPreflight"]
    assert report["planStatus"] == "blocked"
    assert "recognizable" in report["reason"]
    assert report["error"]["type"] == "UsageError"
    assert output["sessionPoolFull"] is False
    assert client.posts == []


def test_autorestore_initial_connection_failure_stays_router_unreachable():
    from bgwcli.errors import RouterConnectionError

    class UnreachableRouter(Router):
        def get_cgi_page(self, page_id, **kwargs):
            raise RouterConnectionError("synthetic offline router")

    client = UnreachableRouter()
    result = run_auto(client, Fetcher(pages(reset=True)))
    assert result.status == "router-unreachable"
    assert result.exit_code == 0
    assert result.allocation_preflight["error"]["type"] == "RouterConnectionError"
    assert client.posts == []


def test_autorestore_auth_after_clear_reports_sent_clear_and_blocked_verification(fast_rescan_clock):
    from bgwcli.errors import RouterAuthError

    class AuthAfterClearRouter(SplitOwnershipRouter):
        def get_cgi_page(self, page_id, **kwargs):
            if self.posts:
                raise RouterAuthError("synthetic expired auth")
            return super().get_cgi_page(page_id, **kwargs)

    client = AuthAfterClearRouter()
    result = run_auto(client, Fetcher(pages(reset=True)))
    report = result.allocation_preflight
    assert result.status == "error"
    assert report["planStatus"] == "blocked"
    assert report["rescanPlanned"] is False
    assert report["clearAttempted"] is True
    assert report["clearResponseReceived"] is True
    assert report["rescanPerformed"] is True
    assert report["ownershipVerified"] is False
    assert report["error"]["type"] == "RouterAuthError"
    assert "pending Clear" not in report["reason"]
    assert len(client.posts) == 1


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
@pytest.mark.parametrize("failure_point", ["nonce", "transport", "fixed"])
def test_real_client_clear_failure_preserves_precise_attempt_evidence(failure_point, automatic, monkeypatch, capsys):
    from urllib.parse import urlsplit

    from bgwcli.client import BGW320Client, RawResponse
    from bgwcli.errors import RouterConnectionError

    requests = []
    device_reads = 0

    def transport(request):
        nonlocal device_reads
        requests.append(request)
        path = urlsplit(request.url).path
        if request.method == "POST":
            raise RouterConnectionError("synthetic ambiguous Clear transport")
        if path.endswith("devices.ha"):
            device_reads += 1
            if failure_point == "nonce" and device_reads > 1:
                raise RouterConnectionError("synthetic nonce read failure")
            body = devices_html([(TARGET_IP, OTHER_MAC, "holder", "off")])
        else:
            body = ipalloc_html([(TARGET_IP, OTHER_MAC, "holder", "off")])
            if failure_point == "fixed":
                body = body.replace("DHCP Allocation", "Fixed Allocation")
        return RawResponse(200, "OK", [], body.encode())

    client = BGW320Client("synthetic.invalid", transport=transport)
    client.import_session(
        {"origin": "https://synthetic.invalid", "authenticated": True, "cookies": {"synthetic": "synthetic"}}
    )

    def fetch(_client, page_ids):
        return {key: value for key, value in pages(reset=True).items() if key in page_ids}, []

    if automatic:
        result = run_auto(client, fetch)
        report = result.allocation_preflight
        # A transport fault before the Clear was sent wrote nothing: the timer stays quiet (exit 0).
        assert (result.status, result.exit_code) == (
            ("router-unreachable", 0) if failure_point == "nonce" else ("error", 2)
        )
    else:
        monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
        command = run_restore(monkeypatch, client, fetch)
        output = json.loads(capsys.readouterr().out)
        assert command.exit_code == 2
        report = output["allocationPreflight"]
    assert report["planStatus"] == "blocked"
    assert report["rescanPlanned"] is False
    assert report["clearAttempted"] is (failure_point == "transport")
    assert report["clearResponseReceived"] is False
    assert report["rescanPerformed"] is False
    assert report["ownershipVerified"] is False
    assert report["error"]["type"] == ("UsageError" if failure_point == "fixed" else "RouterConnectionError")
    assert sum(request.method == "POST" for request in requests) == (1 if failure_point == "transport" else 0)


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_custom_client_failed_clear_reports_unknown_attempt_and_retains_pool_flag(monkeypatch, capsys, automatic):
    from bgwcli.errors import RouterSessionPoolFullError

    class PoolBeforeResponseRouter(SplitOwnershipRouter):
        def post_cgi_page(self, page_id, fields):
            raise RouterSessionPoolFullError("synthetic pool refusal during Clear")

    client = PoolBeforeResponseRouter()
    if automatic:
        output = autorestore.result_output(run_auto(client, Fetcher(pages(reset=True))))
    else:
        monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
        command = run_restore(monkeypatch, client, Fetcher(pages(reset=True)))
        output = json.loads(capsys.readouterr().out)
        assert command.exit_code == 2
    report = output["allocationPreflight"]
    assert report["clearAttempted"] is None
    assert report["clearResponseReceived"] is None
    assert report["rescanPerformed"] is False
    assert report["planStatus"] == "blocked"
    assert report["error"]["type"] == "RouterSessionPoolFullError"
    assert output["sessionPoolFull"] is True
    assert client.posts == []


@pytest.mark.parametrize("phase, response_received", [("initial", False), ("rescan", True)])
def test_real_cli_clear_pool_response_is_recorded_and_starts_session_cooldown(
    monkeypatch, capsys, tmp_env, phase, response_received
):
    from urllib.parse import urlsplit

    from bgwcli.client import BGW320Client, RawResponse
    from bgwcli.errors import RouterSessionPoolFullError
    from bgwcli.session import session_paths

    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "POST":
            assert urlsplit(request.url).path == "/cgi-bin/devices.ha"
            body = "<title>Login</title><p>All web server sessions are in use</p>"
        elif phase == "initial":
            body = "<title>Login</title><p>All web server sessions are in use</p>"
        elif urlsplit(request.url).path.endswith("devices.ha"):
            body = devices_html([(TARGET_IP, OTHER_MAC, "holder", "off")])
        else:
            body = ipalloc_html([(TARGET_IP, OTHER_MAC, "holder", "off")])
        return RawResponse(200, "OK", [], body.encode())

    client = BGW320Client("synthetic.invalid", transport=transport)
    client.import_session(
        {"origin": "https://synthetic.invalid", "authenticated": True, "cookies": {"synthetic": "synthetic"}}
    )
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    monkeypatch.setattr(
        cli,
        "_fetch_snapshot_pages",
        lambda _client, page_ids: ({key: value for key, value in pages(reset=True).items() if key in page_ids}, []),
    )
    command = cli.Command(
        "restore",
        ["synthetic-dump.json"],
        GlobalOptions(json=True),
        commit=phase == "rescan",
        confirm="RESTORE" if phase == "rescan" else None,
    )
    assert cli.run(command, client) == 2
    output = json.loads(capsys.readouterr().out)
    report = output["allocationPreflight"]
    cooldown = session_paths(client.session_identity()).cooldown
    assert [report["clearResponseReceived"], cooldown.exists()] == [response_received, True]
    assert report["clearAttempted"] is (phase == "rescan")
    assert report["rescanPerformed"] is False
    assert report["ownershipVerified"] is False
    assert report["error"]["type"] == "RouterSessionPoolFullError"
    assert output["sessionPoolFull"] is True
    assert sum(request.method == "POST" for request in requests) == (1 if phase == "rescan" else 0)
    deadline = json.loads(cooldown.read_text())["until"]
    prior_requests = len(requests)
    with pytest.raises(RouterSessionPoolFullError, match="cooldown"):
        cli.run(command, client)
    assert len(requests) == prior_requests
    assert json.loads(cooldown.read_text())["until"] == deadline


def test_restore_retains_clear_evidence_when_refreshed_snapshot_extraction_fails(orchestration, monkeypatch, capsys):
    client = Router()
    refreshed = pages(fresh=True)
    refreshed["apphosting"] = apphosting_page(rows=[("ssh", "unresolved-host")], device_options=[])
    fetcher = Fetcher(pages(), refreshed)
    command = run_restore(monkeypatch, client, fetcher)
    output = json.loads(capsys.readouterr().out)
    report = output["allocationPreflight"]
    assert command.exit_code == 2
    assert report["error"]["type"] == "SnapshotExtractionError"
    assert "unresolved-host" in report["error"]["message"]
    assert report["clearAttempted"] is True
    assert report["clearResponseReceived"] is True
    assert report["rescanPerformed"] is True
    assert report["ownershipVerified"] is True
    assert report["planStatus"] == "blocked"
    assert report["planComplete"] is False
    assert output["diff"] is None
    assert output["steps"] == []
    assert client.posts == [("devices", {"Clear": "Clear Device List"})]
    assert fetcher.calls == 2


def test_restore_without_rescan_keeps_its_snapshot_extraction_exception_contract(orchestration, monkeypatch):
    from bgwcli.errors import SnapshotExtractionError

    client = Router(holder_mac=TARGET_MAC)
    malformed = pages()
    malformed["apphosting"] = apphosting_page(rows=[("ssh", "unresolved-host")], device_options=[])
    with pytest.raises(SnapshotExtractionError, match="unresolved-host"):
        run_restore(monkeypatch, client, Fetcher(malformed))
    assert client.posts == []
