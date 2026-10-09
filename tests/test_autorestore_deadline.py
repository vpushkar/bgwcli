"""autorestore's own time limit: no step, pass or inter-pass sleep starts after it, and the service unit's
start timeout is the arithmetic of that limit plus what can still be in flight."""

from __future__ import annotations

from page_builders import ipalloc_page
from test_autorestore import FakeRouter, Fetcher, full_pages, make_dump, reset_pages

from bgwcli import autorestore
from bgwcli.autorestore import (
    CLOSING_FETCH_SECONDS,
    IN_FLIGHT_STEP_SECONDS,
    RUN_DEADLINE_SECONDS,
    UNIT_MARGIN_SECONDS,
    UNIT_START_TIMEOUT_SECONDS,
    AutorestoreOptions,
    run_autorestore,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _run(options, *, fetch_cost=0.0, step_cost=0.0):
    clock = Clock()
    router = FakeRouter()
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages())

    def fetch(client, pages):
        clock.now += fetch_cost
        return fetcher(client, pages)

    def factory():
        router.login()
        return router, False

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    result = run_autorestore(
        factory, make_dump(), options, fetch_pages=fetch, sleep=sleep, log=lambda _: None,
        on_step=lambda _step: setattr(clock, "now", clock.now + step_cost), monotonic=clock,
    )
    return result, router, sleeps


def test_no_step_starts_after_the_run_time_is_up():
    unlimited, unlimited_router, _ = _run(AutorestoreOptions(commit=True, max_passes=1), step_cost=100.0)
    assert len(unlimited_router.posts) >= 3, "the plan has several steps"
    result, router, sleeps = _run(
        AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=150.0), step_cost=100.0
    )
    assert len(router.posts) == 2, "the step in flight finishes, the next one never starts"
    assert result.status == "not-converged" and result.exit_code == 1
    assert len(result.passes) == 1 and sleeps == []
    assert "run time limit of 150s" in result.reason


def test_no_first_step_starts_when_the_initial_reads_already_used_the_run_time():
    """A slow gateway can spend the whole limit on the snapshot reads: the deadline is checked before
    the first step exactly as between steps, so nothing is sent."""
    logs: list[str] = []
    clock = Clock()
    router = FakeRouter()
    fetcher = Fetcher(reset_pages())

    def fetch(client, pages):
        clock.now += 60.0
        return fetcher(client, pages)

    def factory():
        router.login()
        return router, False

    sleeps: list[float] = []
    result = run_autorestore(
        factory, make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=50.0),
        fetch_pages=fetch, sleep=sleeps.append, log=logs.append, monotonic=clock,
    )
    assert router.posts == [], "no write after the limit"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 50s" in result.reason and "next timer run will retry" in result.reason
    assert result.passes == [] and sleeps == []
    assert result.final_diff is not None, "the initial read stays the latest state"
    assert logs[-1].startswith("not-converged:") and "run time limit of 50s" in logs[-1]


def _sent_clear_past_the_limit(tmp_path, monkeypatch, *schedule):
    """The initial read shows a reset gateway, the allocation Clear is sent and takes 100 s of a 50 s limit;
    the post-Clear refresh serves the next page set of `schedule`."""
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.recovery_state import RecoveryCheckpoint

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))
    clock = Clock()
    router = FakeRouter()

    def rescan(client, preflight, *, log, evidence):
        router.posts.append(("ipalloc", {"Clear": "Clear and Rescan for Devices"}))
        evidence.clear_attempted = True
        clock.now += 100.0

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    fetcher = Fetcher(*schedule)

    def factory():
        router.login()
        return router, False

    logs: list[str] = []
    result = run_autorestore(
        factory, make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=50.0),
        fetch_pages=fetcher, sleep=lambda _: None, log=logs.append, monotonic=clock, checkpoint=store,
    )
    return result, router, store, fetcher, logs


def test_a_sent_allocation_clear_is_reported_counted_and_verified_when_the_limit_passed(tmp_path, monkeypatch):
    from bgwcli.restore import restore_converged

    result, router, store, fetcher, _logs = _sent_clear_past_the_limit(
        tmp_path, monkeypatch, reset_pages(), reset_pages()
    )
    assert result.status == "not-converged" and result.exit_code == 1
    assert len(router.posts) == 1, "exactly the Clear; no restore step"
    assert "nothing was sent" not in result.reason and "Clear was already sent" in result.reason
    assert "failure 1/3" in result.reason and store.failure_count() == 1 and store.is_active()
    assert len(fetcher.calls) == 2, "initial read, post-Clear refresh; the refresh is the closing read"
    assert result.final_diff is not None and not restore_converged(result.final_diff, False)
    assert result.passes == []


def test_a_sent_allocation_clear_whose_refresh_shows_the_gateway_converged_ends_converged(tmp_path, monkeypatch):
    result, router, store, fetcher, logs = _sent_clear_past_the_limit(
        tmp_path, monkeypatch, reset_pages(), full_pages()
    )
    assert result.status == "converged" and result.exit_code == 0
    assert "run time limit of 50s" in result.reason and "before the first restore step" in result.reason
    assert "allocation Clear was already sent" in result.reason and "gateway converged" in result.reason
    assert "failure" not in result.reason and "next timer run will retry" not in result.reason
    assert len(router.posts) == 1, "exactly the Clear"
    assert len(fetcher.calls) == 2, "initial read, post-Clear refresh; no third read"
    assert store.failure_count() == 0 and not store.is_active(), "nothing counted, the intent is finished"
    assert result.final_diff is not None and result.passes == []
    assert logs[-1].startswith("converged:")


def test_a_sent_allocation_clear_whose_refresh_leaves_only_blocked_steps_stays_not_converged(tmp_path, monkeypatch):
    """Reservations stay missing and the plan blocks every one of them (the devices are not on the IP
    Allocation page): differences remain, so the run is not-converged and counted like the ordinary path."""
    from bgwcli.restore import restore_converged

    blocked_pages = full_pages()
    blocked_pages["ipalloc"] = ipalloc_page(rows=[])
    result, router, store, fetcher, _logs = _sent_clear_past_the_limit(
        tmp_path, monkeypatch, reset_pages(), blocked_pages
    )
    assert result.final_diff is not None and not restore_converged(result.final_diff, False)
    assert result.plan is not None and all(
        step.kind == "skip" or step.blocked is not None for step in result.plan
    ), "the plan has no executable step"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 50s" in result.reason and "allocation Clear was already sent" in result.reason
    assert "failure 1/3" in result.reason
    assert len(router.posts) == 1 and len(fetcher.calls) == 2
    assert store.failure_count() == 1 and store.is_active()
    assert result.passes == []


# stop_for_deadline's "no refresh" fallback (a closing read when result.final_diff is None) is unreachable
# from run_autorestore: `sent` is only set with the Clear's refresh, which either stores its diff in
# result.final_diff or raises into the allocation-failure return, so no test can drive it.


def test_no_allocation_clear_starts_when_the_initial_reads_already_used_the_run_time(tmp_path, monkeypatch):
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.recovery_state import RecoveryCheckpoint

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))
    clock = Clock()
    router = FakeRouter()

    def rescan(client, preflight, *, log, evidence):
        router.posts.append(("ipalloc", {"Clear": "Clear and Rescan for Devices"}))
        evidence.clear_attempted = True

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    fetcher = Fetcher(reset_pages())

    def fetch(client, pages):
        clock.now += 60.0
        return fetcher(client, pages)

    def factory():
        router.login()
        return router, False

    logs: list[str] = []
    result = run_autorestore(
        factory, make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=50.0),
        fetch_pages=fetch, sleep=lambda _: None, log=logs.append, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [], "no Clear after the limit"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 50s" in result.reason and "nothing was sent" in result.reason
    assert store.failure_count() == 0 and store.is_active(), "nothing counted, intent kept"
    assert len(fetcher.calls) == 1, "the initial read only: no closing read when nothing was sent"
    assert result.final_diff is not None and result.passes == []
    assert logs[-1].startswith("not-converged:")


def test_a_clear_started_inside_the_limit_is_still_counted_and_verified(tmp_path, monkeypatch):
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.recovery_state import RecoveryCheckpoint

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))
    clock = Clock()
    router = FakeRouter()

    def rescan(client, preflight, *, log, evidence):
        router.posts.append(("ipalloc", {"Clear": "Clear and Rescan for Devices"}))
        evidence.clear_attempted = True
        clock.now += 100.0

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    fetcher = Fetcher(reset_pages())

    def factory():
        router.login()
        return router, False

    result = run_autorestore(
        factory, make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=50.0),
        fetch_pages=fetcher, sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert len(router.posts) == 1 and result.status == "not-converged"
    assert "Clear was already sent" in result.reason and store.failure_count() == 1
    assert len(fetcher.calls) == 2, "initial read and the post-Clear refresh, which is the closing read"


def test_the_first_step_still_runs_one_second_inside_the_limit():
    result, router, _ = _run(AutorestoreOptions(commit=True, max_passes=1, max_run_seconds=50.0), fetch_cost=49.0)
    assert router.posts, "inside the limit the run proceeds as before"
    assert "run time limit" not in result.reason


def test_an_unfinished_recovery_intent_is_kept_when_the_limit_passed_before_the_first_step(tmp_env):
    from bgwcli.recovery_state import RecoveryCheckpoint

    dump = make_dump()
    checkpoint = RecoveryCheckpoint("router.local", dump, ())
    checkpoint.begin()
    clock = Clock()
    router = FakeRouter()
    fetcher = Fetcher(reset_pages())

    def fetch(client, pages):
        clock.now += 60.0
        return fetcher(client, pages)

    def factory():
        router.login()
        return router, False

    result = run_autorestore(
        factory, dump, AutorestoreOptions(commit=True, max_run_seconds=50.0),
        fetch_pages=fetch, sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=checkpoint,
    )
    assert router.posts == [] and result.status == "not-converged" and result.exit_code == 1
    assert checkpoint.is_active() and checkpoint.failure_count() == 0, "kept, nothing was sent so nothing counted"


def test_no_pass_or_sleep_starts_when_the_wait_would_cross_the_limit():
    result, router, sleeps = _run(
        AutorestoreOptions(commit=True, max_passes=3, wait_seconds=30, max_run_seconds=50.0), fetch_cost=20.0
    )
    assert len(result.passes) == 1 and sleeps == []
    assert result.status == "not-converged" and "run time limit of 50s" in result.reason


def test_the_default_limit_leaves_normal_runs_alone():
    result, router, sleeps = _run(AutorestoreOptions(commit=True, max_passes=2, wait_seconds=30), fetch_cost=555.0)
    assert len(result.passes) == 2 and sleeps == [30]
    assert "run time limit" not in result.reason


def _conflict_preflight(monkeypatch, router):
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))

    def rescan(client, preflight, *, log, evidence):
        router.posts.append(("ipalloc", {"Clear": "Clear and Rescan for Devices"}))
        evidence.clear_attempted = True

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)


def _slow_begin_store(tmp_path, clock, seconds, *, begun):
    from bgwcli.recovery_state import RecoveryCheckpoint

    class SlowBegin(RecoveryCheckpoint):
        def begin(self):
            super().begin()
            clock.now += seconds

    store = SlowBegin("router.local", make_dump(), None, root=tmp_path / "recovery")
    if begun:
        RecoveryCheckpoint.begin(store)
    return store


def _factory(router):
    def factory():
        router.login()
        return router, False

    return factory


def test_no_allocation_clear_starts_when_checkpoint_preparation_used_the_run_time(tmp_path, monkeypatch):
    clock = Clock()
    router = FakeRouter()
    _conflict_preflight(monkeypatch, router)
    store = _slow_begin_store(tmp_path, clock, 2.0, begun=False)
    fetcher = Fetcher(reset_pages())
    logs: list[str] = []
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=logs.append, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [], "the Clear is not started after the limit"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "before the allocation Clear" in result.reason and "nothing was sent" in result.reason
    assert store.failure_count() == 0 and not store.is_active(), "a fresh unused intent is discarded"
    assert len(fetcher.calls) == 1, "no closing read when nothing was sent"
    assert logs[-1].startswith("not-converged:")


def test_an_unfinished_intent_is_kept_when_checkpoint_preparation_used_the_run_time_before_the_clear(
    tmp_path, monkeypatch
):
    clock = Clock()
    router = FakeRouter()
    _conflict_preflight(monkeypatch, router)
    store = _slow_begin_store(tmp_path, clock, 2.0, begun=True)
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [] and result.status == "not-converged" and result.exit_code == 1
    assert store.is_active() and store.failure_count() == 0
    assert len(fetcher.calls) == 1


def test_no_first_step_starts_when_checkpoint_preparation_used_the_run_time(tmp_path):
    clock = Clock()
    router = FakeRouter()
    store = _slow_begin_store(tmp_path, clock, 2.0, begun=False)
    fetcher = Fetcher(reset_pages())
    logs: list[str] = []
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=logs.append, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [], "no step after the limit"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "before the first restore step" in result.reason and "nothing was sent" in result.reason
    assert result.passes == [] and store.failure_count() == 0 and not store.is_active()
    assert len(fetcher.calls) == 1 and logs[-1].startswith("not-converged:")


def test_an_unfinished_intent_is_kept_when_checkpoint_preparation_used_the_run_time_before_the_first_step(tmp_path):
    clock = Clock()
    router = FakeRouter()
    store = _slow_begin_store(tmp_path, clock, 2.0, begun=True)
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [] and result.status == "not-converged" and result.exit_code == 1
    assert store.is_active() and store.failure_count() == 0
    assert len(fetcher.calls) == 1


def test_no_pass_two_step_starts_when_the_inter_pass_sleep_overran_the_limit(tmp_path, monkeypatch):
    from bgwcli.recovery_state import RecoveryCheckpoint

    executed: list[int] = []
    real_execute = autorestore.execute_restore

    def counting_execute(client, steps, on_step):
        executed.append(len(steps))
        return real_execute(client, steps, on_step)

    monkeypatch.setattr(autorestore, "execute_restore", counting_execute)
    clock = Clock()
    router = FakeRouter()
    fetcher = Fetcher(reset_pages())
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    posts_before_sleep: list[int] = []

    def sleep(seconds):
        sleeps.append(seconds)
        posts_before_sleep.append(len(router.posts))
        clock.now += seconds + 500.0  # the sleep oversleeps far past the limit

    result = run_autorestore(
        _factory(router), make_dump(),
        AutorestoreOptions(commit=True, max_passes=3, wait_seconds=30, max_run_seconds=100.0),
        fetch_pages=fetcher, sleep=sleep, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert sleeps == [30], "the sleep itself started inside the limit"
    assert posts_before_sleep[0] > 0 and len(router.posts) == posts_before_sleep[0], "pass 2 sent nothing"
    assert len(result.passes) == 1 and len(executed) == 1, "pass 2 is neither planned for execution nor run"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 100s" in result.reason
    assert result.final_diff is not None, "the closing read of pass 1 stays"
    assert len(fetcher.calls) == 2, "initial read and pass 1's closing read; no read for pass 2"
    assert store.is_active() and store.failure_count() == 1, "pass-1 writes counted, intent kept"


def _blocked_first_pass(monkeypatch):
    """Pass 1 is entirely blocked (nothing posted); later passes execute for real."""
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    real_execute = autorestore.execute_restore
    calls: list[int] = []

    def execute(client, steps, on_step=None):
        calls.append(len(steps))
        if len(calls) == 1:
            blocked = RestoreStepResult(
                order=1, page="dosprotect", kind="form", description="dosprotect", status="blocked",
                write_attempted=False, error="needs the UI",
            )
            return RestoreExecution(steps=[blocked], stopped_at=None)
        return real_execute(client, steps, on_step)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    return calls


def test_no_pass_two_step_starts_when_the_first_checkpoint_is_prepared_in_pass_two_past_the_limit(
    tmp_path, monkeypatch
):
    from bgwcli.recovery_state import RecoveryCheckpoint

    executed = _blocked_first_pass(monkeypatch)
    clock = Clock()
    router = FakeRouter()
    begins: list[int] = []

    class SlowSecondBegin(RecoveryCheckpoint):
        def begin(self):
            super().begin()
            begins.append(1)
            if len(begins) == 2:  # pass 1 sent nothing and dropped its unused intent: this is the first real publish
                clock.now += 2.0

    store = SlowSecondBegin("router.local", make_dump(), None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset_pages())
    logs: list[str] = []
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=logs.append, monotonic=clock, checkpoint=store,
    )
    assert len(begins) == 2 and len(executed) == 1, "pass 2 prepared its checkpoint but never executed"
    assert router.posts == [], "no write started after the limit"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 1800s" in result.reason and "nothing was sent" in result.reason
    assert "Clear" not in result.reason
    assert store.failure_count() == 0 and not store.is_active(), "the fresh unused intent is discarded"
    assert len(fetcher.calls) == 2, "initial read and pass 1's closing read; no extra closing read"
    assert logs[-1].startswith("not-converged:")


def test_no_pass_two_step_starts_after_a_sent_clear_and_a_fully_blocked_first_pass_when_the_limit_passes(
    tmp_path, monkeypatch
):
    from bgwcli.recovery_state import RecoveryCheckpoint

    executed = _blocked_first_pass(monkeypatch)
    clock = Clock()
    router = FakeRouter()
    _conflict_preflight(monkeypatch, router)
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset_pages())
    logs: list[str] = []

    def log(line):
        logs.append(line)
        if line.startswith("pass 2/"):
            clock.now = 1801.0  # the limit passes while pass 2 is being prepared

    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 100.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=log, monotonic=clock, checkpoint=store,
    )
    assert [page for page, _ in router.posts] == ["ipalloc"], "only the Clear went out; no late write"
    assert len(executed) == 1, "pass 2 was never executed"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 1800s" in result.reason and "failure 1/3" in result.reason
    assert store.failure_count() == 1 and store.is_active(), "the Clear is counted and the intent kept"
    assert len(result.passes) == 1 and result.final_diff is not None, "pass 1's closing diff stays"
    assert any("earlier writes in this run were sent" in line for line in logs)


def test_no_later_pass_step_starts_when_pass_two_logging_used_the_limit_after_an_earlier_save(tmp_path, monkeypatch):
    from bgwcli.recovery_state import RecoveryCheckpoint

    real_execute = autorestore.execute_restore
    executed: list[int] = []

    def execute(client, steps, on_step=None):
        executed.append(len(steps))
        if len(executed) == 1:  # pass 1 sends exactly one Save
            steps = [step for step in steps if step.page == "dosprotect"]
        return real_execute(client, steps, on_step)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    clock = Clock()
    router = FakeRouter()
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset_pages())
    logs: list[str] = []

    def log(line):
        logs.append(line)
        if line.startswith("pass 2/"):
            clock.now = 1801.0  # a stalled progress write

    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 100.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=log, monotonic=clock, checkpoint=store,
    )
    assert len(router.posts) == 1 and len(executed) == 1, "pass 2 sent and executed nothing"
    assert result.status == "not-converged" and result.exit_code == 1
    assert "run time limit of 1800s" in result.reason and "Clear" not in result.reason
    assert "failure 1/3" in result.reason and store.failure_count() == 1 and store.is_active()
    assert len(result.passes) == 1 and result.final_diff is not None, "pass 1's closing diff stays"
    assert len(fetcher.calls) == 2, "initial read and pass 1's closing read; no read for pass 2"


def test_a_first_step_started_inside_the_limit_after_checkpoint_preparation_still_runs(tmp_path):
    clock = Clock()
    router = FakeRouter()
    store = _slow_begin_store(tmp_path, clock, 1.0, begun=False)
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_passes=1, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1798.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert router.posts, "1799 s is inside the limit: the step starts"
    assert "run time limit" not in result.reason


def test_the_unit_start_timeout_is_the_limit_plus_what_can_still_be_in_flight():
    assert RUN_DEADLINE_SECONDS == 1800.0
    assert IN_FLIGHT_STEP_SECONDS == 15 + 15 + 60, "nonce read, POST, acknowledgement window"
    assert CLOSING_FETCH_SECONDS == 37 * 15, "upper bound: 11 snapshot pages at the page timeout"
    assert UNIT_START_TIMEOUT_SECONDS == 1800 + 90 + 555 + UNIT_MARGIN_SECONDS == 2580
    assert autorestore.AutorestoreOptions(commit=True).max_run_seconds == RUN_DEADLINE_SECONDS


def test_a_step_started_inside_the_limit_finishes_its_wifi_warning_continue_after_the_limit(
    tmp_env, clock, monkeypatch, capsys
):
    """The real client over a scripted wire: the Wi-Fi Save is POSTed inside the limit, the run's time
    runs out before the Warning's Continue POST, and that Continue is still sent (the step is one save).
    The next step never starts and the run ends with the deadline shape."""
    import json

    from save_helpers import SAVED_RED, client_with, form, html

    from bgwcli import cli
    from bgwcli.dumpfile import write_dump_file
    from bgwcli.snapshot import Snapshot, SnapshotMeta

    run_clock = Clock()
    monkeypatch.setattr(autorestore, "_monotonic", run_clock)
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(
        SnapshotMeta("", "", "router.local"),
        forms={"wconfig": {"setting": "new"}, "etherlan": {"setting": "new"}},
    ))
    warn_page = (
        '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
        '<input type="submit" name="Continue" value="Continue"></form>'
    )
    state = {"confirmed": False, "continues": 0, "saves": 0}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            return html("", 302, {"location": "/cgi-bin/home.ha"})
        if request.method == "POST":
            if b"Continue" in request.body:
                state["continues"] += 1
                state["confirmed"] = True
                return html("", 302, {"location": "/cgi-bin/wconfig.ha"})
            state["saves"] += 1
            run_clock.now = 1801.0  # the limit passes while the step is in flight
            return html("", 302, {"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if request.url.endswith("wifiwarn_advanced.ha"):
            return html(warn_page)
        if "wconfig" in request.url:
            if state["confirmed"]:
                return html(form("wconfig", "new", banner=SAVED_RED))
            return html(form("wconfig", "old"))
        return html(form("etherlan", "old"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
    monkeypatch.setattr(autorestore, "_sleep", lambda _s: None)
    code = cli.main([
        "autorestore", str(path), "--include", "wconfig,etherlan", "--host", "router.local",
        "--commit", "--confirm", "RESTORE", "--json",
    ])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert [("Continue" in r.body.decode()) for r in posts] == [False, True], "Save, then its Continue"
    assert state["saves"] == 1 and state["continues"] == 1
    assert code == 1 and out["status"] == "not-converged"
    assert "the run time limit of 1800s was reached" in out["reason"]
    assert len(out["passes"]) == 1 and out["passes"][0]["applied"] == 1
    assert not any("etherlan" in r.url for r in posts), "the next step never started"
