"""autorestore classes the client's refusal of a bad nonce source page as structural (exit 2).

The client re-reads the page a write's nonce comes from and refuses, before anything is sent, a
Login, Page-not-found, parser-cut or control-less page and a page that yields no nonce. That is a
finding about the gateway's answer, not a lost connection: the run ends `error` (exit 2), the refusal
is counted against the recovery intent like any structural failure, and nothing is retried quietly
every timer run. A transport fault keeps its classification. Every test counts the POSTs sent."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html
from test_autorestore import FakeRouter, Fetcher, make_dump, reset_pages

from bgwcli import autorestore, cli
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.client import NoncePageRefusedError, RouterResponseError
from bgwcli.dumpfile import write_dump_file
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.restore import RestoreExecution, RestoreStepResult
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"
WAIT = "<html><head><title>Please wait</title></head><body>Please wait...</body></html>"
NOT_FOUND = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"
NO_NONCE = form(PAGE, "old").replace('<input name="nonce" value="abc123">', "")


def _run(tmp_env, monkeypatch, capsys, second_read, *, with_intent=True):
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    checkpoint = RecoveryCheckpoint("router.local", dump, (PAGE,))
    if with_intent:
        checkpoint.begin()  # a recovery already under way: the run's failure is counted against it
    sleeps: list[float] = []
    monkeypatch.setattr(autorestore, "_sleep", sleeps.append)
    state = {"gets": 0}

    def handler(request, number):
        if request.method == "POST":
            if "login.ha" in request.url:
                return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
            raise AssertionError("a configuration POST was sent")
        state["gets"] += 1
        if state["gets"] == 1:
            return html(form(PAGE, "old"))
        if isinstance(second_read, Exception):
            raise second_read
        return html(second_read)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
    code = cli.main([
        "autorestore", str(path), "--include", PAGE, "--host", "router.local", "--commit", "--confirm", "RESTORE",
        "--max-passes", "3", "--wait", "120", "--json",
    ])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "login.ha" not in r.url]
    return code, out, sleeps, posts, checkpoint


@pytest.mark.parametrize(
    ("second_read", "reason"),
    [
        pytest.param(WAIT, "no form controls", id="please-wait"),
        pytest.param(NOT_FOUND, "page-not-found", id="page-not-found"),
        pytest.param(NO_NONCE, "no write nonce found", id="no-nonce"),
    ],
)
def test_a_refused_nonce_page_on_the_first_step_is_a_counted_structural_error(
    tmp_env, monkeypatch, capsys, clock, second_read, reason
):
    code, out, sleeps, posts, checkpoint = _run(tmp_env, monkeypatch, capsys, second_read)
    assert code == 2 and out["status"] == "error"
    assert posts == [], "nothing was sent"
    assert len(out["passes"]) == 1 and sleeps == [], "no further pass, no inter-pass sleep"
    step = out["passes"][0]["steps"][0]
    assert step["writeAttempted"] is False and step["writePerformed"] is False
    assert step["errorType"] == "NoncePageRefusedError"
    assert PAGE in out["reason"] and reason in out["reason"], "the page and the reason are named"
    assert "failure 1/3" in out["reason"]
    assert out["writeUnanswered"] is False
    assert checkpoint.is_active() and checkpoint.failure_count() == 1, "counted, intent kept"


def test_a_refused_nonce_page_without_a_recovery_intent_still_exits_2(tmp_env, monkeypatch, capsys, clock):
    """The existing rule for a structural pre-send read failure: exit 2 every run, counted only
    against an intent that exists; the run's own unused intent is not left behind."""
    code, out, sleeps, posts, checkpoint = _run(tmp_env, monkeypatch, capsys, WAIT, with_intent=False)
    assert code == 2 and out["status"] == "error" and posts == [] and sleeps == []
    assert not checkpoint.is_active() and checkpoint.failure_count() == 0, "nothing to count against"


def test_a_transport_fault_on_the_first_nonce_read_stays_router_unreachable(tmp_env, monkeypatch, capsys, clock):
    code, out, sleeps, posts, checkpoint = _run(tmp_env, monkeypatch, capsys, TimeoutError("nonce read timed out"))
    assert code == 0 and out["status"] == "router-unreachable"
    assert posts == [] and sleeps == [] and len(out["passes"]) == 1
    assert checkpoint.is_active() and checkpoint.failure_count() == 0, "uncounted, intent kept"


def test_an_http_error_on_the_first_nonce_read_stays_router_unreachable(tmp_env, monkeypatch, capsys, clock):
    code, out, sleeps, posts, checkpoint = _run(tmp_env, monkeypatch, capsys, RouterResponseError("HTTP 503"))
    assert code == 0 and out["status"] == "router-unreachable" and posts == []


def _two_pass_run(tmp_path, fields):
    calls = {"n": 0}

    def execute(client, steps, on_step=None):
        calls["n"] += 1
        base = {"order": 1, "page": PAGE, "kind": "form", "description": PAGE}
        if calls["n"] == 1:
            step = RestoreStepResult(**base, status="unchanged", write_attempted=True, write_response_received=True)
        else:
            step = RestoreStepResult(**base, status="failed", write_attempted=False, write_performed=False, **fields)
        return RestoreExecution(steps=[step], stopped_at=None if calls["n"] == 1 else 1)

    return calls, execute


def test_a_refused_nonce_page_after_an_earlier_write_ends_the_run_as_a_counted_error(monkeypatch, tmp_path):
    calls, execute = _two_pass_run(tmp_path, {
        "error_type": "NoncePageRefusedError",
        "error": "dosprotect: the page read for the write nonce has no form controls",
    })
    monkeypatch.setattr(autorestore, "execute_restore", execute)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages(), reset_pages(), reset_pages(), reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2
    assert calls["n"] == 2 and len(result.passes) == 2, "no pass 3"
    assert sleeps == [1], "only the pause between pass 1 and pass 2; none after the refusal"
    assert "no form controls" in result.reason and PAGE in result.reason
    assert store.failure_count() == 1 and store.is_active(), "the earlier write is counted"


def test_a_transport_pre_send_fault_after_an_earlier_write_is_still_not_converged(monkeypatch, tmp_path):
    calls, execute = _two_pass_run(tmp_path, {"error_type": "RouterConnectionError", "error": "timed out"})
    monkeypatch.setattr(autorestore, "execute_restore", execute)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=Fetcher(reset_pages(), reset_pages(), reset_pages(), reset_pages()), sleep=sleeps.append,
        log=lambda _: None, checkpoint=store,
    )
    assert result.status == "not-converged" and result.exit_code == 1
    assert calls["n"] == 2 and sleeps == [1]
    assert store.failure_count() == 1 and store.is_active()


def test_the_client_refusal_is_a_distinct_response_error():
    assert issubclass(NoncePageRefusedError, RouterResponseError)


def test_a_refused_allocation_clear_is_a_counted_structural_error(tmp_path, monkeypatch):
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_args, **_kw: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))

    def rescan(client, preflight, *, log, evidence):
        # The client's nonce-page validation refuses the devices page before the Clear is sent.
        raise NoncePageRefusedError("devices: the page read for the write nonce has no form controls")

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True), fetch_pages=Fetcher(reset_pages()),
        checkpoint=store, log=lambda _: None,
    )
    assert (result.status, result.exit_code) == ("error", 2)
    assert router.posts == [], "no POST was sent"
    assert "no form controls" in result.reason
    assert result.allocation_preflight["clearAttempted"] is False
    assert store.is_active() and store.failure_count() == 1


def test_a_transport_fault_before_the_allocation_clear_stays_router_unreachable(tmp_path, monkeypatch):
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.errors import RouterConnectionError

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_args, **_kw: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))

    def rescan(client, preflight, *, log, evidence):
        raise RouterConnectionError("timed out")

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True), fetch_pages=Fetcher(reset_pages()),
        checkpoint=store, log=lambda _: None,
    )
    assert (result.status, result.exit_code) == ("router-unreachable", 0) and router.posts == []
    assert store.failure_count() == 0
