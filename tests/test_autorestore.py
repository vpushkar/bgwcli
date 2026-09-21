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
from bgwcli.snapshot_diff import EntryDiff, FormFieldDiff, ReservationDiff, SnapshotDiff
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


def test_on_any_diff_flags_any_difference_but_not_identical():
    dump = dump_snapshot()
    detected, reason = detect_factory_reset(a_diff(extra_services=[SSH]), dump, on_any_diff=True)
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
    """Records POSTs; pages are served by the fetch_pages fake below, so this only needs post_cgi_page."""

    def __init__(self):
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.logins = 0

    def login(self):
        self.logins += 1

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        return HttpResponse(200, "OK", {}, "<html><title>ok</title></html>", f"https://router.local/cgi-bin/{page}.ha")

    def get_cgi_page(self, page, *, auth=True):  # pragma: no cover - deferred forwards only
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
    assert fetcher.calls == [("sysinfo", "services", "apphosting", "ipalloc", "packetfilter", "dosprotect", "wconfig", "wconfig_unified")]
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


def test_router_unreachable_at_login_is_quiet_and_exit_0():
    router = FakeRouter()

    def factory():
        raise RouterConnectionError("connect EHOSTUNREACH")

    logs: list[str] = []
    result = run_autorestore(factory, make_dump(), AutorestoreOptions(commit=True), fetch_pages=Fetcher(), sleep=_no_sleep, log=logs.append)
    assert result.status == "router-unreachable" and result.exit_code == 0
    assert result.reason == "connect EHOSTUNREACH" and router.posts == []
    assert logs == ["router-unreachable: connect EHOSTUNREACH"]


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


def test_used_fallback_code_alone_counts_as_a_reset_signal():
    router = FakeRouter()
    logs: list[str] = []
    result = run_autorestore(
        lambda: (router, True), make_dump(), AutorestoreOptions(commit=False), fetch_pages=Fetcher(full_pages()), sleep=_no_sleep, log=logs.append
    )
    assert result.used_fallback_code is True and result.detected is True
    assert result.status == "restore-needed" and result.exit_code == 1
    assert "access code reverted; factory reset suspected" in logs


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
