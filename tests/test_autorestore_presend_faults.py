"""autorestore ends the run on ANY fault that arrives before a configuration POST, not only a transport one.

A session-wide auth failure, a page-level 401/403 on the nonce read, a full session pool or any other
non-transport error before the first POST ends the run as `error` (exit 2) naming the failure: no
further pass, no inter-pass sleep, no second login handshake, nothing counted (nothing was sent) and
the recovery intent kept. A transport fault keeps the quiet router-unreachable exit 0 rule. Every test
here drives the real client over the in-memory wire and counts the POSTs (and login POSTs) it sent."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html
from test_autorestore import FakeRouter, Fetcher, _canned_execution, _no_sleep, make_dump, reset_pages

from bgwcli import autorestore, cli
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.dumpfile import write_dump_file
from bgwcli.errors import RouterConnectionError
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"
LOGIN = (
    '<title>Login</title><form action="/cgi-bin/login.ha"><input name="nonce" value="abc">'
    '<input type="password" name="password"></form>'
)
POOL_FULL = "<title>Login</title><p>All web server sessions are in use</p>"


def _nonce_read_answers(kind: str):
    """The first GET (the snapshot read) is the dosprotect page; every later GET (the nonce read of the
    write step) answers `kind`."""

    def answer(request):
        if kind == "login":
            return html(LOGIN)
        if kind == "pool-full":
            return html(POOL_FULL)
        if kind == "forbidden":
            return html("<title>Forbidden</title>", status=403)
        if kind == "transport":
            raise TimeoutError("nonce read timed out")
        raise AssertionError(kind)

    return answer


def _run(tmp_env, monkeypatch, capsys, kind):
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    sleeps: list[float] = []
    monkeypatch.setattr(autorestore, "_sleep", sleeps.append)
    state = {"gets": 0}
    answer = _nonce_read_answers(kind)

    def handler(request, number):
        if request.method == "POST":
            if "login.ha" in request.url:
                return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
            raise AssertionError("a configuration POST was sent")
        state["gets"] += 1
        return html(form(PAGE, "old")) if state["gets"] == 1 else answer(request)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
    code = cli.main([
        "autorestore", str(path), "--include", PAGE, "--host", "router.local", "--commit", "--confirm", "RESTORE",
        "--max-passes", "3", "--wait", "120", "--json",
    ])
    out = json.loads(capsys.readouterr().out)
    checkpoint = RecoveryCheckpoint("router.local", dump, (PAGE,))
    record = json.loads(checkpoint.path.read_text()) if checkpoint.path.exists() else None
    posts = [r for r in wire.requests if r.method == "POST"]
    return {
        "exit": code, "out": out, "sleeps": sleeps, "record": record,
        "config_posts": sum("login.ha" not in r.url for r in posts),
        "login_posts": sum("login.ha" in r.url for r in posts),
    }


@pytest.mark.parametrize("kind", ["login", "forbidden", "pool-full"])
def test_a_non_transport_fault_on_the_nonce_read_ends_the_run_as_an_error(tmp_env, monkeypatch, capsys, clock, kind):
    run = _run(tmp_env, monkeypatch, capsys, kind)
    out = run["out"]
    assert run["exit"] == 2 and out["status"] == "error"
    assert len(out["passes"]) == 1, "no further pass"
    assert run["sleeps"] == [], "no inter-pass sleep"
    assert run["config_posts"] == 0
    assert run["login_posts"] <= 1, "no second login attempt by autorestore"
    assert out["writeUnanswered"] is False
    assert out["passes"][0]["steps"][0]["writeAttempted"] is False
    assert run["record"] is None, "nothing was sent: no intent is left behind and nothing is counted"
    assert "pass 1" in out["reason"] and "before the dosprotect write was sent" in out["reason"]


def test_each_fault_class_is_named_in_the_reason(tmp_env, monkeypatch, capsys, clock):
    login = _run(tmp_env, monkeypatch, capsys, "login")["out"]["reason"]
    assert "login page" in login.lower()


def test_pool_full_on_the_nonce_read_is_reported_as_pool_full(tmp_env, monkeypatch, capsys, clock):
    out = _run(tmp_env, monkeypatch, capsys, "pool-full")["out"]
    assert out["sessionPoolFull"] is True
    assert "session pool was full before the dosprotect write was sent" in out["reason"]


def test_a_transport_fault_on_the_nonce_read_stays_router_unreachable(tmp_env, monkeypatch, capsys, clock):
    run = _run(tmp_env, monkeypatch, capsys, "transport")
    assert run["exit"] == 0 and run["out"]["status"] == "router-unreachable"
    assert len(run["out"]["passes"]) == 1 and run["sleeps"] == [] and run["config_posts"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"session_pool_full": True, "error_type": "RouterSessionPoolFullError", "error": "pool full"},
        {"error_type": "RouterAuthError", "error": "Router returned the login page"},
        {"error_type": "RouterAuthError", "error_page_level": True, "error": "HTTP 403"},
        {"error_type": "SnapshotExtractionError", "error": "no nonce on the page"},
    ],
)
def test_a_pre_send_fault_after_an_earlier_send_ends_the_run_and_counts(monkeypatch, tmp_path, fields):
    """Pass 1 posted, pass 2 hits the fault before its POST: the run ends (no stale replan) and the run
    counts, because a write did go out in it."""
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    calls = {"n": 0}

    def execute(client, steps, on_step=None):
        calls["n"] += 1
        base = {"order": 1, "page": "dosprotect", "kind": "form", "description": "dosprotect"}
        if calls["n"] == 1:
            step = RestoreStepResult(**base, status="unchanged", write_attempted=True, write_response_received=True)
        else:
            step = RestoreStepResult(**base, status="failed", write_attempted=False, **fields)
        return RestoreExecution(steps=[step], stopped_at=None if calls["n"] == 1 else 1)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages(), reset_pages(), reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2
    assert calls["n"] == 2 and len(result.passes) == 2
    assert store.failure_count() == 1 and store.is_active()


@pytest.mark.parametrize(
    "fields",
    [
        {"error_type": "RouterConnectionError", "error": "connection timed out"},
        {"error_type": "RouterResponseError", "error": "HTTP 503"},
    ],
)
def test_a_transport_pre_send_fault_after_an_earlier_send_ends_the_run_not_converged(monkeypatch, tmp_path, fields):
    """Pass 1 posted, pass 2's nonce read fails on the transport before its POST: the run ends at once
    (no pass 3, no second inter-pass sleep), `not-converged` exit 1, the sent write counted, the closing
    read still made and the recovery intent kept."""
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    calls = {"n": 0}

    def execute(client, steps, on_step=None):
        calls["n"] += 1
        base = {"order": 1, "page": "dosprotect", "kind": "form", "description": "dosprotect"}
        if calls["n"] == 1:
            step = RestoreStepResult(**base, status="unchanged", write_attempted=True, write_response_received=True)
        else:
            step = RestoreStepResult(**base, status="failed", write_attempted=False, **fields)
        return RestoreExecution(steps=[step], stopped_at=None if calls["n"] == 1 else 1)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages(), reset_pages(), reset_pages(), reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "not-converged" and result.exit_code == 1
    assert calls["n"] == 2 and len(result.passes) == 2
    assert sleeps == [1]
    # The initial read, pass 1's closing read and the closing read after the failed pass 2.
    assert len(fetcher.calls) == 3
    assert store.failure_count() == 1 and store.is_active()


def test_a_failed_closing_read_after_a_transport_pre_send_fault_is_the_counted_verification_failure(
    monkeypatch, tmp_path
):
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    calls = {"n": 0}

    def execute(client, steps, on_step=None):
        calls["n"] += 1
        base = {"order": 1, "page": "dosprotect", "kind": "form", "description": "dosprotect"}
        if calls["n"] == 1:
            step = RestoreStepResult(**base, status="unchanged", write_attempted=True, write_response_received=True)
        else:
            step = RestoreStepResult(
                **base, status="failed", write_attempted=False, error_type="RouterConnectionError", error="timed out"
            )
        return RestoreExecution(steps=[step], stopped_at=None if calls["n"] == 1 else 1)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    # The initial read and pass 1's closing read succeed; pass 2's closing read fails.
    fetcher = Fetcher(reset_pages(), reset_pages(), RouterConnectionError("closing read failed"))
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and calls["n"] == 2 and sleeps == [1]
    assert "verification unavailable" in result.reason
    assert store.failure_count() == 1 and store.is_active()


def test_a_non_transport_pre_send_fault_without_a_recovery_checkpoint_keeps_nothing(monkeypatch):
    _canned_execution(monkeypatch, write_attempted=False, error_type="RouterAuthError", error="login page")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None,
    )
    assert result.status == "error" and sleeps == [] and len(fetcher.calls) == 1 and len(result.passes) == 1


def test_an_unfinished_recovery_intent_is_kept_and_not_counted_by_a_pre_send_fault(monkeypatch, tmp_path):
    _canned_execution(monkeypatch, write_attempted=False, error_type="RouterAuthError", error="login page")
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=Fetcher(reset_pages()), sleep=_no_sleep, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and store.is_active() and store.failure_count() == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"error_type": "RouterAuthError", "error": "Router returned the login page"},
        {"error_type": "RouterAuthError", "error_page_level": True, "error": "HTTP 403"},
        {"session_pool_full": True, "error_type": "RouterSessionPoolFullError", "error": "pool full"},
    ],
)
def test_an_acknowledged_write_whose_verification_read_loses_the_session_is_verification_unavailable(
    monkeypatch, tmp_path, fields
):
    _canned_execution(
        monkeypatch, write_attempted=True, write_response_received=True, write_performed=True,
        acknowledgement_observed=True, **fields,
    )
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2 and "verification-unavailable" in result.reason
    assert sleeps == [] and len(fetcher.calls) == 1 and store.failure_count() == 1
