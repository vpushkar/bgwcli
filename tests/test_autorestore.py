"""autorestore: factory-reset detection matrix and the restore loop, driven with fakes (no gateway)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from page_builders import apphosting_page, dosprotect_page, ipalloc_page, page, select, services_page, sysinfo_page

from bgwcli.autorestore import (
    AutorestoreOptions,
    AutorestoreResult,
    ResetSignature,
    detect_factory_reset,
    result_output,
    run_autorestore,
)
from bgwcli.errors import RouterConnectionError
from bgwcli.fetch import ParsedPageResult
from bgwcli.snapshot import (
    Snapshot,
    SnapshotForward,
    SnapshotMeta,
    SnapshotReservation,
    SnapshotService,
    extract_snapshot,
)
from bgwcli.snapshot_diff import EntryDiff, FormFieldDiff, ReservationDiff, SnapshotDiff, diff_snapshots
from bgwcli.types import HttpResponse

META = SnapshotMeta(firmware="4.27.7", ts="2026-09-20T00:00:00.000Z", router_host="router.local")
SSH = SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")
MOSH = SnapshotService("Mosh", 60001, 60010, 60001, "UDP")
FWD_SSH = SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01")
FWD_MOSH = SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")
RES_A = SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64")
RES_B = SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.70")
FORM_CHANGE = [FormFieldDiff(field="flood_protect", dump="on", live="off")]


def _no_sleep(_seconds: int) -> None:
    raise AssertionError("sleep must not be called")


def dump_snapshot(**overrides) -> Snapshot:
    base = {
        "services": [SSH, MOSH],
        "forwards": [FWD_SSH, FWD_MOSH],
        "reservations": [RES_A, RES_B],
        "forms": {"dosprotect": {"flood_protect": "on"}, "wconfig": {"ssid": "EXAMPLE-NET"}},
    }
    base.update(overrides)
    return Snapshot(meta=META, **base)


def a_diff(
    *,
    services_missing=(),
    forwards_missing=(),
    reservations_missing=(),
    forms=None,
    extra_services=(),
) -> SnapshotDiff:
    forms = dict(forms or {})
    identical = not (services_missing or forwards_missing or reservations_missing or forms or extra_services)
    return SnapshotDiff(
        identical=identical,
        services=EntryDiff(missing=list(services_missing), extra=list(extra_services)),
        forwards=EntryDiff(missing=list(forwards_missing)),
        reservations=ReservationDiff(missing=list(reservations_missing)),
        forms=forms,
        firmware_changed=False,
    )


# ---------------------------------------------------------------------------------------------
# detection matrix


def test_all_three_sections_missing_is_a_factory_reset():
    dump = dump_snapshot()
    diff = a_diff(services_missing=[SSH, MOSH], forwards_missing=[FWD_SSH, FWD_MOSH], reservations_missing=[RES_A, RES_B])
    detected, reason = detect_factory_reset(diff, dump)
    assert detected is True
    assert "services 2/2 missing" in reason and "forwards 2/2 missing" in reason and "reservations 2/2 missing" in reason


def test_only_some_entries_missing_is_ordinary_drift():
    dump = dump_snapshot()
    partial = a_diff(services_missing=[SSH], forwards_missing=[FWD_SSH, FWD_MOSH], reservations_missing=[RES_A, RES_B])
    assert detect_factory_reset(partial, dump) == (False, "services 1/2 missing; forwards 2/2 missing; reservations 2/2 missing")
    one_section = a_diff(services_missing=[SSH, MOSH])
    detected, reason = detect_factory_reset(one_section, dump)
    assert detected is False and "forwards 0/2 missing" in reason


def test_identical_diff_is_never_a_reset():
    detected, reason = detect_factory_reset(a_diff(), dump_snapshot())
    assert detected is False and reason == "no differences"


def test_sections_the_dump_never_had_are_ignored():
    # No forwards and no reservations in the dump: only the services section can vote.
    dump = dump_snapshot(forwards=[], reservations=[])
    detected, reason = detect_factory_reset(a_diff(services_missing=[SSH, MOSH]), dump)
    assert detected is True
    assert "forwards" not in reason and "reservations" not in reason


def test_forms_only_dump_falls_back_to_every_form_page_differing():
    dump = dump_snapshot(services=[], forwards=[], reservations=[])
    both = a_diff(forms={"dosprotect": FORM_CHANGE, "wconfig": [FormFieldDiff("ssid", "EXAMPLE-NET", "ATT-xyz")]})
    detected, reason = detect_factory_reset(both, dump)
    assert detected is True and "2/2 form pages differ" in reason
    one = a_diff(forms={"dosprotect": FORM_CHANGE})
    assert detect_factory_reset(one, dump) == (False, "1/2 form pages differ")


def test_empty_dump_can_never_signal_a_reset():
    dump = dump_snapshot(services=[], forwards=[], reservations=[], forms={})
    detected, reason = detect_factory_reset(a_diff(extra_services=[SSH]), dump)
    assert detected is False and "nothing" in reason


def test_pages_selection_narrows_the_sections_that_vote():
    dump = dump_snapshot()
    diff = a_diff(services_missing=[SSH, MOSH])
    assert detect_factory_reset(diff, dump)[0] is False
    assert detect_factory_reset(diff, dump, pages=("services",))[0] is True


def test_on_any_diff_flags_any_restorable_difference_but_not_identical():
    dump = dump_snapshot()
    detected, reason = detect_factory_reset(a_diff(services_missing=[SSH]), dump, on_any_diff=True)
    assert detected is True and "on-any-diff" in reason
    assert detect_factory_reset(a_diff(), dump, on_any_diff=True) == (False, "no differences")


def test_reset_signature_counts():
    dump = dump_snapshot()
    diff = a_diff(services_missing=[SSH], reservations_missing=[RES_A, RES_B], forms={"dosprotect": FORM_CHANGE})
    signature = ResetSignature.from_diff(diff, dump)
    assert signature.missing == {"services": 1, "forwards": 0, "reservations": 2, "forms": 1}
    assert signature.totals == {"services": 2, "forwards": 2, "reservations": 2, "forms": 2}


# ---------------------------------------------------------------------------------------------
# run loop


class FakeRouter:
    """Records POSTs and exposes an empty ownership table; snapshots use the fetcher below."""

    def __init__(self):
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.logins = 0

    def login(self):
        self.logins += 1

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        from integration_html import saved_configuration_html
        body = saved_configuration_html(page, fields)
        return HttpResponse(200, "OK", {}, body, f"https://router.local/cgi-bin/{page}.ha")

    def get_cgi_page(self, page, *, auth=True):
        if page == "devices":
            body = (
                '<html><head><title>Device List</title></head><body><table>'
                '<tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Status</th></tr>'
                '</table><input type="submit" name="Clear" value="Clear and Rescan for Devices"></body></html>'
            )
            return HttpResponse(200, "OK", {}, body, "https://router.local/cgi-bin/devices.ha")
        if page == "ipalloc":
            body = (
                '<html><head><title>IP Allocation</title></head><body><table>'
                '<tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Status</th><th>Allocation</th></tr>'
                '</table></body></html>'
            )
            return HttpResponse(200, "OK", {}, body, "https://router.local/cgi-bin/ipalloc.ha")
        raise AssertionError("unused")


def full_pages():
    return {
        "sysinfo": sysinfo_page(),
        "services": services_page(),
        "apphosting": apphosting_page(),
        "ipalloc": ipalloc_page(),
        "dosprotect": dosprotect_page(selects=[select("flood_protect", [("on", "On", True), ("off", "Off")])]),
        "packetfilter": page("packetfilter"),
        "wconfig": page("wconfig"),
        "wconfig_unified": page("wconfig_unified"),
    }


def reset_pages():
    pages = full_pages()
    pages["services"] = services_page(rows=[])
    pages["apphosting"] = apphosting_page(rows=[])
    pages["ipalloc"] = ipalloc_page(rows=[])
    pages["dosprotect"] = dosprotect_page(selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)])])
    return pages


def make_dump() -> Snapshot:
    return extract_snapshot(full_pages(), ts=META.ts, router_host=META.router_host)


class Fetcher:
    """fetch_pages fake: serves `schedule[n]` on the n-th call (last entry repeats), tracks the call log."""

    def __init__(self, *schedule):
        self.schedule = list(schedule)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, client, pages):
        self.calls.append(tuple(pages))
        state = self.schedule[min(len(self.calls) - 1, len(self.schedule) - 1)]
        if isinstance(state, Exception):
            raise state
        parsed = {p: state[p] for p in pages if p in state}
        failures = [ParsedPageResult(p, False, error=f"{p}.ha timed out") for p in pages if p not in state]
        return parsed, failures


def harness(fetcher, options, **kwargs):
    router = FakeRouter()
    sleeps: list[int] = []
    logs: list[str] = []

    def factory():
        router.login()
        return router, False

    result = run_autorestore(
        factory,
        make_dump(),
        options,
        fetch_pages=fetcher,
        sleep=sleeps.append,
        log=logs.append,
        **kwargs,
    )
    return result, router, sleeps, logs


def test_no_reset_when_the_router_matches_the_dump():
    fetcher = Fetcher(full_pages())
    result, router, sleeps, logs = harness(fetcher, AutorestoreOptions(commit=True))
    assert result.status == "no-reset" and result.exit_code == 0 and result.detected is False
    assert result.missing == {"services": 0, "forwards": 0, "reservations": 0, "forms": 0}
    assert router.posts == [] and sleeps == [] and router.logins == 1
    assert fetcher.calls == [("sysinfo", "services", "apphosting", "ipalloc", "packetfilter", "dosprotect", "wconfig")]
    assert any("no-reset" in line for line in logs)


def test_ordinary_drift_is_left_alone_even_with_commit():
    pages = full_pages()
    pages["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    result, router, _, _ = harness(Fetcher(pages), AutorestoreOptions(commit=True))
    assert result.status == "no-reset" and result.exit_code == 0
    assert result.missing["services"] == 1 and router.posts == []
    assert result.final_diff is not None and not result.final_diff.identical


def test_restore_needed_dry_run_plans_but_never_posts():
    result, router, sleeps, _ = harness(Fetcher(reset_pages()), AutorestoreOptions(commit=False))
    assert result.status == "restore-needed" and result.exit_code == 1 and result.detected is True
    assert router.posts == [] and sleeps == [] and result.passes == []
    kinds = {step.kind for step in result.plan}
    assert "add-service" in kinds and "form" in kinds
    payload = result_output(result)
    assert payload["status"] == "restore-needed" and payload["exitCode"] == 1
    assert all("rawPayload" not in step for step in payload["plan"])


def test_commit_converges_on_pass_two_and_sleeps_once():
    fetcher = Fetcher(reset_pages(), reset_pages(), full_pages())
    result, router, sleeps, logs = harness(fetcher, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=7))
    assert result.status == "converged" and result.exit_code == 0
    assert len(result.passes) == 2 and result.passes[-1]["converged"] is True
    assert sleeps == [7]  # only between pass 1 and pass 2, never after convergence
    assert len(fetcher.calls) == 3
    assert result.passes[0]["applied"] >= 1 and result.passes[0]["failed"] == 0
    assert any("pass 1/3" in line for line in logs) and any("converged" in line for line in logs)
    assert result.final_diff is not None and result.final_diff.identical


def test_commit_never_prunes_router_only_entries():
    pages = full_pages()
    pages["services"] = services_page(
        rows=[("custom_ssh", "2483-2483", "22", "TCP"), ("Mosh", "60001-60010", "60001", "UDP"), ("Extra", "5-5", "5", "TCP")]
    )
    fetcher = Fetcher(reset_pages(), pages)
    result, router, _, _ = harness(fetcher, AutorestoreOptions(commit=True))
    assert result.status == "converged"
    assert not any(step.kind.startswith("remove") for step in result.plan)
    assert not any("Remove" in key for _, payload in router.posts for key in payload)


def test_not_converged_after_max_passes():
    fetcher = Fetcher(reset_pages())
    result, router, sleeps, logs = harness(fetcher, AutorestoreOptions(commit=True, max_passes=2, wait_seconds=30))
    assert result.status == "not-converged" and result.exit_code == 1
    assert len(result.passes) == 2 and sleeps == [30]
    assert router.posts and len(fetcher.calls) == 3
    assert any("not-converged" in line for line in logs)


@pytest.mark.parametrize("restored_after_timeout", [False, True], ids=["still-missing", "now-matching"])
def test_save_acknowledgement_timeout_stops_all_passes_but_keeps_the_final_read_only_diff(
    monkeypatch, restored_after_timeout
):
    from bgwcli import restore

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class UnconfirmedRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            self.posts.append((page, dict(fields)))
            return HttpResponse(200, "OK", {}, "<p>Processing configuration</p>", f"https://router.local/{page}")

        def get_cgi_page(self, page, **kwargs):
            return HttpResponse(200, "OK", {}, "<p>Processing configuration</p>", f"https://router.local/{page}")

    clock = Clock()
    monkeypatch.setattr(restore, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore, "sleep", clock.sleep)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_POLL_SECONDS", 1.0)
    router = UnconfirmedRouter()
    after = full_pages() if restored_after_timeout else reset_pages()
    fetcher = Fetcher(reset_pages(), after)
    between_pass_sleeps = []
    logs = []

    result = run_autorestore(
        lambda: (router, False),
        make_dump(),
        AutorestoreOptions(commit=True, max_passes=3, wait_seconds=7, pages=("services",)),
        fetch_pages=fetcher,
        sleep=between_pass_sleeps.append,
        log=logs.append,
    )

    # The Save was sent and its reply never carried "Changes saved": no answer, whatever the closing diff shows.
    assert result.status == "error" and result.exit_code == 2 and result.write_unanswered is True
    assert len(result.passes) == 1
    assert result.passes[0]["failed"] == 1 and result.passes[0]["stoppedAt"] == 1
    assert result.passes[0]["converged"] is False
    assert len(router.posts) == 1  # The uncertain Add must never be automatically repeated.
    assert between_pass_sleeps == []
    assert len(fetcher.calls) == 2  # Detection read plus the final read-only verification.
    assert result.final_diff is not None
    assert result.final_diff.identical is restored_after_timeout
    assert "Changes saved" in result.reason
    assert any("Changes saved" in line for line in logs)


def test_router_unreachable_at_login_is_quiet_and_exit_0():
    router = FakeRouter()

    def factory():
        raise RouterConnectionError("connect EHOSTUNREACH")

    logs: list[str] = []
    result = run_autorestore(factory, make_dump(), AutorestoreOptions(commit=True), fetch_pages=Fetcher(), sleep=_no_sleep, log=logs.append)
    assert result.status == "router-unreachable" and result.exit_code == 0
    assert result.reason == "connect EHOSTUNREACH" and router.posts == []
    assert logs == ["router-unreachable: connect EHOSTUNREACH"]


def test_router_http_error_at_login_is_router_unreachable():
    """An HTTP error status from the login handshake or a page fetch (gateway web UI half-up)
    before any POST is the same quiet "router-unreachable" outcome as a refused connection."""
    from bgwcli.client import RouterResponseError

    router = FakeRouter()

    def factory():
        raise RouterResponseError("Router rejected https://gw/cgi-bin/login.ha with HTTP 500.", status_code=500)

    logs: list[str] = []
    result = run_autorestore(factory, make_dump(), AutorestoreOptions(commit=True), fetch_pages=Fetcher(), sleep=_no_sleep, log=logs.append)
    assert result.status == "router-unreachable" and result.exit_code == 0 and router.posts == []
    assert logs == ["router-unreachable: Router rejected https://gw/cgi-bin/login.ha with HTTP 500."]


def test_page_fetch_failures_before_any_post_are_router_unreachable():
    pages = full_pages()
    pages.pop("services")
    result, router, _, logs = harness(Fetcher(pages), AutorestoreOptions(commit=True))
    assert result.status == "router-unreachable" and result.exit_code == 0 and router.posts == []
    assert "services" in result.reason and len(logs) == 1


def test_page_fetch_failure_after_a_post_is_an_error():
    broken = reset_pages()
    broken.pop("apphosting")
    fetcher = Fetcher(reset_pages(), broken)
    result, router, sleeps, _ = harness(fetcher, AutorestoreOptions(commit=True, max_passes=3))
    assert result.status == "error" and result.exit_code == 2
    assert router.posts and sleeps == [] and "apphosting" in result.reason


def _drift_pages():
    drift = full_pages()
    drift["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    return drift


@pytest.mark.parametrize("pages", [full_pages, _drift_pages], ids=["matching", "ordinary-drift"])
@pytest.mark.parametrize("commit", [False, True])
def test_used_fallback_code_alone_never_turns_ordinary_drift_into_a_reset(pages, commit):
    """The sticker code also works when someone simply set the access code back by hand: without a
    reset-shaped diff (or an unfinished recovery) the run leaves the router alone and warns."""
    router = FakeRouter()
    logs: list[str] = []
    result = run_autorestore(
        lambda: (router, True), make_dump(), AutorestoreOptions(commit=commit), fetch_pages=Fetcher(pages()),
        sleep=_no_sleep, log=logs.append,
    )
    assert result.used_fallback_code is True and result.detected is False
    assert result.status == "no-reset" and result.exit_code == 0 and router.posts == []
    assert len(result.warnings) == 1 and "BGW_ACCESS_CODE" in result.warnings[0]
    assert result_output(result)["warnings"] == result.warnings
    assert any(line.startswith("warning: ") for line in logs)


def test_used_fallback_code_with_a_reset_shaped_diff_names_the_access_code_reversal():
    router = FakeRouter()
    logs: list[str] = []
    result = run_autorestore(
        lambda: (router, True), make_dump(), AutorestoreOptions(commit=False), fetch_pages=Fetcher(reset_pages()),
        sleep=_no_sleep, log=logs.append,
    )
    assert result.detected is True and result.status == "restore-needed" and result.exit_code == 1
    assert result.reason.startswith("access code reverted; factory reset suspected; ")
    assert result.warnings == []
    assert "access code reverted; factory reset suspected" in logs


def test_used_fallback_code_during_an_unfinished_recovery_resumes_it(tmp_path):
    from bgwcli.recovery_state import RecoveryCheckpoint

    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    store.begin()
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, True), make_dump(), AutorestoreOptions(commit=False), fetch_pages=Fetcher(_drift_pages()),
        sleep=_no_sleep, log=lambda _: None, checkpoint=store,
    )
    assert result.detected is True and result.status == "restore-needed"
    assert "resuming unfinished recovery" in result.reason


def test_on_any_diff_option_restores_ordinary_drift():
    drift = full_pages()
    drift["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    fetcher = Fetcher(drift, full_pages())
    result, router, _, _ = harness(fetcher, AutorestoreOptions(commit=True, on_any_diff=True))
    assert result.status == "converged" and router.posts and router.posts[0][0] == "services"


def test_pages_option_restricts_the_plan():
    fetcher = Fetcher(reset_pages(), reset_pages(), reset_pages())
    result, router, _, _ = harness(fetcher, AutorestoreOptions(commit=True, max_passes=1, pages=("services",)))
    assert result.status == "not-converged"
    assert {p for p, _ in router.posts} == {"services"}


@pytest.mark.parametrize(
    ("status", "code"),
    [("no-reset", 0), ("restore-needed", 1), ("converged", 0), ("not-converged", 1), ("router-unreachable", 0), ("error", 2)],
)
def test_exit_code_table(status, code):
    assert AutorestoreResult(status=status, reason="x").exit_code == code


def test_result_output_shape_is_json_friendly():
    fetcher = Fetcher(reset_pages(), full_pages())
    result, _, _, _ = harness(fetcher, AutorestoreOptions(commit=True))
    payload = result_output(result)
    assert payload["status"] == "converged" and payload["exitCode"] == 0 and payload["detected"] is True
    assert payload["usedFallbackCode"] is False
    assert payload["missing"] == {"services": 2, "forwards": 2, "reservations": 2, "forms": 1}
    assert payload["passes"][0].keys() >= {"pass", "applied", "blocked", "failed", "skipped", "notRun", "converged"}
    assert payload["diff"]["identical"] is True
    assert "finalDiff" not in payload and "plan" in payload
    assert replace(result, final_diff=None).final_diff is None


def test_autorestore_pool_metadata_stays_paired_with_the_reported_failure():
    from bgwcli.client import session_pool_full_error

    class PoolRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            if self.posts:
                raise session_pool_full_error(waited_ms=2300, retry_count=4)
            return super().post_cgi_page(page, fields)

    router = PoolRouter()
    fetcher = Fetcher(reset_pages(), full_pages())
    result = run_autorestore(
        lambda: (router, False), make_dump(),
        AutorestoreOptions(commit=True, max_passes=3, pages=("services",)),
        fetch_pages=fetcher, sleep=_no_sleep, log=lambda _: None,
    )
    assert len(fetcher.calls) == 1, "no closing read (a second login) against a full pool"
    # A write that may have gone out and met a full pool got no answer: exit 2 either way.
    assert result.status == "error" and result.exit_code == 2
    assert result.write_unanswered is True
    assert len(result.passes) == 1 and result.passes[0]["applied"] == 1
    assert result.session_pool_full is True
    assert (result.waited_ms, result.retry_count) == (2300, 4)
    failed = result.passes[0]["steps"][1]
    assert failed["status"] == "failed" and failed["sessionPoolFull"] is True
    assert (failed["waitedMs"], failed["retryCount"]) == (2300, 4)


@pytest.mark.parametrize("fault", ["http", "timeout"])
def test_allocation_preflight_read_failure_before_any_write_is_router_unreachable(fault):
    from bgwcli.client import RouterResponseError

    class Router(FakeRouter):
        def get_cgi_page(self, page, *, auth=True):
            if page == "devices":
                if fault == "http":
                    raise RouterResponseError(
                        "Router rejected https://router.local/cgi-bin/devices.ha with HTTP 503.", status_code=503, url="x"
                    )
                raise RouterConnectionError("Timed out reading response from devices.ha")
            return super().get_cgi_page(page, auth=auth)

    router = Router()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages()), sleep=lambda _: None, log=lambda _: None,
    )
    assert router.posts == []
    assert result.status == "router-unreachable" and result.exit_code == 0
    assert "allocation preflight failed" in (result.reason or "")


# ---------------------------------------------------------------------------------------------
# differences restore cannot act on never look like a reset


def _live_only_select_pages():
    pages = full_pages()
    pages["dosprotect"] = dosprotect_page(
        selects=[
            select("flood_protect", [("on", "On", True), ("off", "Off")]),
            select("newfw_opt", [("a", "A", True), ("b", "B")]),
        ]
    )
    return pages


def test_a_live_only_control_neither_votes_for_a_reset_nor_triggers_on_any_diff():
    base = make_dump()
    dump = replace(base, services=[], forwards=[], reservations=[], forms={"dosprotect": base.forms["dosprotect"]})
    diff = diff_snapshots(dump, extract_snapshot(_live_only_select_pages(), ts="", router_host=""))
    assert not diff.identical
    assert ResetSignature.from_diff(diff, dump).missing["forms"] == 0
    for on_any_diff in (False, True):
        detected, reason = detect_factory_reset(diff, dump, on_any_diff=on_any_diff)
        assert detected is False, reason
        assert reason == "nothing restore can act on differs"


def test_router_only_entries_do_not_trigger_on_any_diff():
    # autorestore never prunes, so a router-only entry is nothing it can act on.
    detected, reason = detect_factory_reset(a_diff(extra_services=[SSH]), dump_snapshot(), on_any_diff=True)
    assert (detected, reason) == (False, "nothing restore can act on differs")


def test_autorestore_with_only_a_live_only_control_is_no_reset_and_posts_nothing(tmp_path, monkeypatch):
    from save_helpers import install_clock

    from bgwcli.recovery_state import RecoveryCheckpoint

    install_clock(monkeypatch)
    base = make_dump()
    dump = replace(base, services=[], forwards=[], reservations=[], forms={"dosprotect": base.forms["dosprotect"]})
    checkpoint = RecoveryCheckpoint("http://router.local", dump, None, root=tmp_path / "state")
    for commit, on_any_diff in ((False, False), (True, False), (True, True), (True, False)):
        router = FakeRouter()
        result = run_autorestore(
            lambda router=router: (router, False),
            dump,
            AutorestoreOptions(commit=commit, max_passes=3, wait_seconds=1, on_any_diff=on_any_diff),
            fetch_pages=Fetcher(_live_only_select_pages()),
            sleep=lambda s: None,
            log=lambda line: None,
            checkpoint=checkpoint,
        )
        assert (result.status, result.exit_code, len(router.posts)) == ("no-reset", 0, 0)
        assert not checkpoint.is_active()


# ---------------------------------------------------------------------------------------------
# an acknowledged write is never re-posted within one invocation


def _posted_entries(router):
    from collections import Counter

    def entry(page, fields):
        if page == "services":
            return (page, fields.get("Service"))
        if page == "apphosting":
            return (page, fields.get("service"))
        return (page, None)

    return Counter(entry(page, fields) for page, fields in router.posts)


def test_acknowledged_writes_that_never_take_effect_are_posted_once_across_all_passes():
    """The gateway answers every Save with its saved-configuration page but the values never stick:
    pass 1 posts each step once; passes 2 and 3 only re-verify (and retry nothing never posted)."""
    fetcher = Fetcher(reset_pages())
    result, router, sleeps, logs = harness(fetcher, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=5))
    assert result.status == "not-converged" and result.exit_code == 1
    # Nothing is sent in pass 2, so there is nothing for the gateway to settle before pass 3.
    assert len(result.passes) == 3 and sleeps == [5]
    posted = _posted_entries(router)
    assert posted and set(posted.values()) == {1}, posted
    assert {page for page, _ in posted} == {"services", "apphosting", "dosprotect"}
    assert [p["applied"] for p in result.passes] == [result.passes[0]["applied"], 0, 0]
    assert result.passes[0]["applied"] >= 3
    later = [step for p in result.passes[1:] for step in p["steps"] if step["page"] not in ("ipalloc", "packetfilter")]
    assert later and all(step["status"] == "skipped" for step in later)
    assert all("not re-posted" in step["description"] for step in later)
    assert "the next timer run will retry" in result.reason


def test_a_step_blocked_in_pass_one_is_retried_in_a_later_pass_but_applied_ones_are_not():
    no_save = reset_pages()
    no_save["dosprotect"] = dosprotect_page(
        selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)])], save_button=None
    )
    fetcher = Fetcher(no_save, reset_pages(), full_pages())
    result, router, sleeps, _ = harness(fetcher, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=5))
    assert result.status == "converged" and len(result.passes) == 2
    posted = _posted_entries(router)
    assert set(posted.values()) == {1}, posted
    assert ("dosprotect", None) in posted
    pass_one = {s["page"]: s["status"] for s in result.passes[0]["steps"] if s["kind"] == "form"}
    pass_two = {s["page"]: s["status"] for s in result.passes[1]["steps"] if s["kind"] == "form"}
    assert pass_one["dosprotect"] == "blocked" and pass_two["dosprotect"] == "applied"


# --- a configuration write with no answer is exit 2, even when the closing diff is readable ---------


def _dosprotect_autorestore(tmp_env, monkeypatch, capsys, post_reply):
    import json

    from save_helpers import client_with, form, html

    from bgwcli import autorestore, cli
    from bgwcli.dumpfile import write_dump_file

    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    state = {"posted": False}

    def handler(request, _number):
        if request.method == "POST":
            state["posted"] = True
            return post_reply(html)
        return html(form("dosprotect", "old", banner=state.pop("banner", "")))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    monkeypatch.setattr(autorestore, "_sleep", lambda _: None)
    code = cli.main(
        ["autorestore", str(path), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json"]
    )
    output = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" for r in wire.requests)
    return code, output, posts


def _raise(error):
    def reply(_html):
        raise error

    return reply


@pytest.mark.parametrize(
    "case",
    ["timeout", "http-503", "login-redirect", "pool-full"],
)
def test_autorestore_unanswered_write_is_an_error_even_with_a_readable_closing_diff(
    tmp_env, monkeypatch, capsys, clock, case
):
    replies = {
        "timeout": _raise(TimeoutError("synthetic config timeout")),
        "http-503": lambda html: html("busy", 503),
        "login-redirect": lambda html: html("", 302, {"location": "/cgi-bin/login.ha"}),
        "pool-full": lambda html: html("<title>Login</title><p>All web server sessions are in use</p>"),
    }
    code, output, posts = _dosprotect_autorestore(tmp_env, monkeypatch, capsys, replies[case])
    assert code == 2 and output["exitCode"] == 2
    assert output["status"] == "error"
    assert output["writeUnanswered"] is True
    assert "dosprotect" in output["reason"] and "no answer" in output["reason"]
    assert posts == 1


def test_autorestore_explicit_rejection_stays_not_converged_exit_1(tmp_env, monkeypatch, capsys, clock):
    from save_helpers import ERROR

    def rejected(html):
        return html(ERROR + "<p>rejected</p>")

    code, output, posts = _dosprotect_autorestore(tmp_env, monkeypatch, capsys, rejected)
    assert code == 1 and output["status"] == "not-converged"
    assert output["writeUnanswered"] is False
    assert posts == 1


def test_unanswered_write_step_classifies_step_outcomes():
    from bgwcli.autorestore import unanswered_write_step
    from bgwcli.restore import WRITE_REJECTED_PREFIX, RestoreStepResult

    def step(**fields):
        base = {"order": 1, "page": "dosprotect", "kind": "form", "description": "dosprotect", "status": "failed"}
        return RestoreStepResult(**{**base, **fields})

    timeout = step(write_attempted=True, write_response_received=False, error_type="RouterConnectionError")
    verification_lost = step(write_attempted=True, write_response_received=True, error_type="RouterResponseError")
    unknown = step(write_attempted=None, error_type="RouterConnectionError")
    unexpected_status = step(write_attempted=True, write_response_received=True, status_code=500)
    rejected = step(write_attempted=True, write_response_received=True, status_code=302,
                    error=f"{WRITE_REJECTED_PREFIX}bad value")
    no_change = step(write_attempted=True, write_response_received=True, write_performed=False, status_code=302)
    saved_mismatch = step(write_attempted=True, write_response_received=True, write_performed=True,
                          acknowledgement_observed=True, status_code=302)
    never_sent = step(write_attempted=False, error_type="RouterConnectionError")
    applied = step(status="applied", write_attempted=True, write_response_received=True)
    for answered in (rejected, no_change, saved_mismatch, never_sent, applied):
        assert unanswered_write_step([answered]) is None
    for lost in (timeout, verification_lost, unknown, unexpected_status):
        assert unanswered_write_step([applied, lost]) is lost


def _canned_execution(monkeypatch, **step_fields):
    from bgwcli import autorestore
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    base = {"order": 1, "page": "dosprotect", "kind": "form", "description": "dosprotect", "status": "failed"}

    def execute(client, steps, on_step=None):
        return RestoreExecution(steps=[RestoreStepResult(**{**base, **step_fields})], stopped_at=1)

    monkeypatch.setattr(autorestore, "execute_restore", execute)


def test_an_unanswered_lan_move_write_counts_toward_the_limit(tmp_path, monkeypatch):
    from bgwcli.recovery_state import RecoveryCheckpoint

    _canned_execution(
        monkeypatch, status="reconnect-required", write_attempted=True, write_response_received=False,
        lan_address_changed=True, error="connection lost",
    )
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    for expected in (1, 2):
        result = run_autorestore(
            lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=2),
            fetch_pages=Fetcher(reset_pages()), sleep=_no_sleep, log=lambda _: None, checkpoint=store,
        )
        assert result.status == "error" and store.failure_count() == expected
    stopped = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=2),
        fetch_pages=Fetcher(reset_pages()), sleep=_no_sleep, log=lambda _: None, checkpoint=store,
    )
    assert stopped.status == "error" and "consecutive runs" in stopped.reason and store.failure_count() == 3


def test_an_acknowledged_lan_move_reconnect_never_counts_toward_the_limit(tmp_path, monkeypatch):
    from bgwcli.recovery_state import RecoveryCheckpoint

    _canned_execution(
        monkeypatch, status="reconnect-required", write_attempted=True, write_response_received=True,
        lan_address_changed=True, reconnect_address="192.168.2.254", error="reconnect",
    )
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    for _ in range(4):
        result = run_autorestore(
            lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=2),
            fetch_pages=Fetcher(reset_pages()), sleep=_no_sleep, log=lambda _: None, checkpoint=store,
        )
        assert result.status == "error" and "consecutive runs" not in result.reason
        assert store.failure_count() == 0 and store.is_active(), "the intent is left, uncounted"


def test_pool_full_before_any_write_is_not_an_unanswered_write(monkeypatch):
    _canned_execution(monkeypatch, write_attempted=False, session_pool_full=True, error="all sessions in use")
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=2),
        fetch_pages=Fetcher(reset_pages()), sleep=_no_sleep, log=lambda _: None,
    )
    assert result.write_unanswered is False and result.session_pool_full is True
    assert result.status == "error" and result.exit_code == 2
    assert "before the dosprotect write was sent" in result.reason


def test_a_pass_that_sent_nothing_does_not_wait_before_the_next_pass(monkeypatch):
    _canned_execution(monkeypatch, status="blocked", write_attempted=False, error="needs the UI")
    sleeps: list[int] = []
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=9),
        fetch_pages=Fetcher(reset_pages()), sleep=sleeps.append, log=lambda _: None,
    )
    assert result.status == "not-converged" and sleeps == []


@pytest.mark.parametrize("structural, status, code", [(True, "error", 2), (False, "router-unreachable", 0)])
def test_structural_fetch_failures_are_errors_but_transport_failures_stay_unreachable(structural, status, code):
    def fetch(client, pages):
        parsed = {p: page for p, page in full_pages().items() if p in pages and p != "services"}
        return parsed, [ParsedPageResult("services", False, error="no services table", structural=structural)]

    router = FakeRouter()
    logs: list[str] = []
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True),
        fetch_pages=fetch, sleep=_no_sleep, log=logs.append,
    )
    assert (result.status, result.exit_code) == (status, code) and router.posts == []
    assert logs == [f"{status}: services: no services table"]
