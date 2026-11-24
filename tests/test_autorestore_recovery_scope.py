"""A recovery intent covers only what was missing or differing when the reset was detected, and it ends
on its own: after RECOVERY_IDLE_RUN_LIMIT consecutive runs that sent nothing, or once it is older than
RECOVERY_MAX_AGE_SECONDS. Before this, an intent whose last step stayed blocked (a forward to an offline
device) stayed open forever, and every later deliberate change in the gateway UI was reverted as
"resuming recovery"."""

import json

from page_builders import apphosting_page, dosprotect_page, select
from test_autorestore import FakeRouter, Fetcher, full_pages, make_dump, reset_pages

from bgwcli import recovery_state
from bgwcli.autorestore import (
    RECOVERY_IDLE_RUN_LIMIT,
    RECOVERY_MAX_AGE_SECONDS,
    AutorestoreOptions,
    recovery_scope,
    run_autorestore,
)
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

FLOOD_OFF = dosprotect_page(selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)])])


def _store(tmp_path):
    return RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")


def _run(pages, store, *, commit=True, max_passes=1):
    router = FakeRouter()
    logs: list[str] = []
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=commit, max_passes=max_passes),
        fetch_pages=Fetcher(pages), checkpoint=store, log=logs.append, sleep=lambda _s: None,
    )
    return result, router, logs


def _offline_forward_pages():
    """Everything restored except the Mosh -> host-a forward, whose device is offline (not offered)."""
    pages = full_pages()
    pages["apphosting"] = apphosting_page(
        rows=[("custom_ssh", "host-b")], device_options=(("aa:bb:cc:dd:ee:01", "host-b"),)
    )
    return pages


def _stuck_store(tmp_path):
    """An open recovery that covers only the offline forward (what a reset recovery leaves behind)."""
    pages = _offline_forward_pages()
    live = extract_snapshot(pages, ts="", router_host="router.local")
    scope = recovery_scope(diff_snapshots(make_dump(), live))
    assert scope["forwards"] and not scope["forms"] and not scope["services"]
    store = _store(tmp_path)
    store.begin(scope=scope)
    return store


def _record(store):
    return json.loads(store.path.read_text())


def test_a_new_recovery_records_what_it_covers(tmp_path):
    store = _store(tmp_path)
    result, router, _ = _run(reset_pages(), store)
    assert result.detected and router.posts, "the reset was detected and restore steps were sent"
    scope = _record(store)["scope"]
    assert scope["forms"] == {"dosprotect": ["flood_protect"]}
    assert len(scope["services"]) == len(make_dump().services)
    assert len(scope["forwards"]) == len(make_dump().forwards)
    assert "flood_protect" in json.dumps(scope) and "on" not in scope["forms"]["dosprotect"], "names, never values"


def test_a_resumed_recovery_never_reverts_a_later_ui_change(tmp_path):
    store = _stuck_store(tmp_path)
    # Days later the user deliberately turns flood protection off in the gateway UI.
    pages = _offline_forward_pages()
    pages["dosprotect"] = FLOOD_OFF
    result, router, _ = _run(pages, store)
    assert router.posts == [], "the UI change is outside the recovery: nothing is posted"
    assert result.status == "not-converged" and result.detected
    assert store.is_active() and _record(store)["idleRuns"] == 1


def test_a_recovery_whose_scope_is_done_ends_converged_despite_later_drift(tmp_path):
    store = _stuck_store(tmp_path)
    pages = full_pages()  # the forward is back (device online again) ...
    pages["dosprotect"] = FLOOD_OFF  # ... and the user changed something else meanwhile
    result, router, _ = _run(pages, store)
    assert result.status == "converged" and router.posts == []
    assert not store.is_active()


def test_consecutive_runs_that_send_nothing_drop_the_intent(tmp_path):
    store = _stuck_store(tmp_path)
    pages = _offline_forward_pages()
    for number in range(1, RECOVERY_IDLE_RUN_LIMIT):
        result, router, _ = _run(pages, store)
        assert router.posts == [] and store.is_active()
        assert f"run {number}/{RECOVERY_IDLE_RUN_LIMIT} of this recovery that sent nothing" in result.reason
    result, router, logs = _run(pages, store)
    assert router.posts == [] and not store.is_active()
    assert result.warnings and "recovery dropped" in result.warnings[0]
    assert any(line.startswith("warning: recovery dropped") for line in logs)
    # The next run sees one forward missing out of two: ordinary drift, left alone.
    pages["dosprotect"] = FLOOD_OFF
    result, router, _ = _run(pages, store)
    assert result.status == "no-reset" and router.posts == []


def test_a_run_that_sends_resets_the_idle_count(tmp_path):
    store = _stuck_store(tmp_path)
    store.note_run(sent=False)
    store.note_run(sent=False)
    assert store.note_run(sent=True) == 0 and _record(store)["idleRuns"] == 0


def test_an_intent_older_than_the_age_limit_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_state, "_now", lambda: 1_000_000.0)  # begun long ago
    store = _stuck_store(tmp_path)
    monkeypatch.undo()
    pages = _offline_forward_pages()
    pages["dosprotect"] = FLOOD_OFF
    result, router, _ = _run(pages, store)
    assert router.posts == [] and not store.is_active()
    assert result.status == "no-reset"
    assert any("recovery dropped" in w and f"{RECOVERY_MAX_AGE_SECONDS // 3600} h limit" in w for w in result.warnings)


def test_a_dry_run_reports_an_expired_intent_but_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery_state, "_now", lambda: 1_000_000.0)
    store = _stuck_store(tmp_path)
    monkeypatch.undo()
    result, router, _ = _run(_offline_forward_pages(), store, commit=False)
    assert router.posts == [] and store.is_active()
    assert any(w.startswith("recovery would be dropped") for w in result.warnings)


def test_an_older_record_without_scope_or_start_loads_and_gets_a_start_time(tmp_path):
    store = _store(tmp_path)
    store.begin()
    record = _record(store)
    for key in ("scope", "startedAt", "idleRuns"):
        record.pop(key, None)
    store.path.write_text(json.dumps(record))  # the v0.3.1 record shape
    assert store.is_active() and store.recorded_scope() is None and store.started_at() is None
    pages = _offline_forward_pages()
    pages["dosprotect"] = FLOOD_OFF
    result, router, _ = _run(pages, store)
    # No scope recorded: the whole difference is the recovery, as before; it now expires from today.
    assert ("dosprotect", {"flood_protect": "on"}) in [(p, {k: v for k, v in f.items() if k == "flood_protect"})
                                                       for p, f in router.posts]
    assert store.started_at() is not None


def test_a_normal_recovery_still_converges_and_clears_the_intent(tmp_path):
    store = _store(tmp_path)
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages(), full_pages()), checkpoint=store, log=lambda _l: None,
        sleep=lambda _s: None,
    )
    assert result.status == "converged" and router.posts and not store.is_active()
