"""A LAN move the gateway answers but never applies counts toward autorestore's cross-run limit.

Codex review (v0.4.0): every tick read the old LAN address, posted the same dhcpserver move, got an
empty 302 and exited 2, while the failure count stayed 0 (any HTTP answer to the move bypassed the
failure record). The answered move is now remembered in the recovery record; a later tick that
reads the moved fields still at their old values counts one failure (the same fingerprint each
time) before it may post again, so the existing 3-identical-failures stop applies.
"""

import dataclasses
import json

from test_autorestore import FakeRouter, Fetcher, make_dump, reset_pages

from bgwcli import autorestore
from bgwcli.autorestore import RETRY_LIMIT, AutorestoreOptions, run_autorestore
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.restore import RestoreExecution, RestoreStepResult
from bgwcli.snapshot_diff import FormFieldDiff

NEW_LAN = "192.168.2.254"


def _lan_still_old(monkeypatch, still_old):
    """Every live diff carries dhcpserver.ipaddr at its old value while `still_old()` is true."""
    real = autorestore.diff_snapshots

    def diff(dump, live, **kwargs):
        result = real(dump, live, **kwargs)
        if still_old():
            forms = {**result.forms, "dhcpserver": [FormFieldDiff("ipaddr", NEW_LAN, "192.168.1.254")]}
            result = dataclasses.replace(result, forms=forms, identical=False)
        return result

    monkeypatch.setattr(autorestore, "diff_snapshots", diff)


def _answered_move(monkeypatch):
    executions: list[int] = []
    step = {
        "order": 1, "page": "dhcpserver", "kind": "form", "description": "dhcpserver", "status": "reconnect-required",
        "write_attempted": True, "write_response_received": True, "status_code": 302,
        "lan_address_changed": True, "reconnect_address": NEW_LAN, "error": "reconnect",
    }

    def execute(client, steps, on_step=None):
        executions.append(1)
        return RestoreExecution(steps=[RestoreStepResult(**step)], stopped_at=1)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    return executions


def _tick(store):
    return run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=2),
        fetch_pages=Fetcher(reset_pages()), sleep=lambda _s: None, log=lambda _: None, checkpoint=store,
    )


def test_an_answered_lan_move_that_never_applies_stops_at_the_retry_limit(tmp_path, monkeypatch):
    _lan_still_old(monkeypatch, lambda: True)
    executions = _answered_move(monkeypatch)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")

    first = _tick(store)
    assert first.status == "error" and store.failure_count() == 0, "the move's own run is never counted"
    counts = []
    for _ in range(RETRY_LIMIT - 1):
        _tick(store)
        counts.append(store.failure_count())
    assert counts == list(range(1, RETRY_LIMIT))
    stopped = _tick(store)
    assert stopped.status == "error" and "consecutive runs" in stopped.reason
    assert store.failure_count() == RETRY_LIMIT
    assert len(executions) == RETRY_LIMIT, "the limit run posts nothing"


def test_a_lan_move_that_applied_is_never_counted(tmp_path, monkeypatch):
    moved = {"done": False}
    _lan_still_old(monkeypatch, lambda: not moved["done"])
    _answered_move(monkeypatch)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    _tick(store)
    moved["done"] = True  # the next tick reads the moved fields at their dumped values
    _tick(store)
    assert store.failure_count() == 0
    record = json.loads(store.path.read_text()) if store.path.exists() else {}
    assert "pendingLanMove" not in record


def test_an_older_record_without_a_pending_move_still_loads(tmp_path):
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    assert store.pending_lan_move() is None
    store.set_pending_lan_move("dhcpserver", ["ipaddr"])
    assert store.pending_lan_move() == {"page": "dhcpserver", "fields": ["ipaddr"]}
    store.begin()  # resuming the same intent keeps it
    assert store.pending_lan_move() == {"page": "dhcpserver", "fields": ["ipaddr"]}
    store.clear_pending_lan_move()
    assert store.pending_lan_move() is None
