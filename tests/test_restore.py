"""Ported from tests/restore.test.ts. Live pages come from page_builders; the executor's page
re-reads go through the `restore._parse_page` seam, monkeypatched here to map body tokens to pages."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from page_builders import (
    apphosting_page,
    button,
    checkbox,
    dosprotect_page,
    field,
    form,
    hidden,
    ipalloc_page,
    page,
    select,
    services_page,
)

from bgwcli import restore
from bgwcli.restore import (
    FORM_SAVE_BUTTONS,
    RESTORE_PAGE_ORDER,
    RestoreDeferredForward,
    RestoreFollowUp,
    RestoreOptions,
    RestoreStep,
    build_restore_plan,
    execute_restore,
    follow_up_payload,
    restore_converged,
)
from bgwcli.snapshot import (
    UNCHECKED,
    Snapshot,
    SnapshotForward,
    SnapshotMeta,
    SnapshotReservation,
    SnapshotService,
    extract_snapshot,
)
from bgwcli.snapshot_diff import (
    EntryDiff,
    FormFieldDiff,
    ReservationChange,
    ReservationDiff,
    SnapshotDiff,
    diff_snapshots,
)
from bgwcli.types import ParsedPage

OPTIONS = RestoreOptions(prune=False, include_lan=False, include_secrets=False)
PRUNE = replace(OPTIONS, prune=True)

CUSTOM_SSH = ("custom_ssh", "2483-2483", "22", "TCP")
STALE = ("Stale", "1-1", "1", "TCP")


def default_dosprotect(*, disabled: bool | None = None, save: str | None = "Save") -> ParsedPage:
    return dosprotect_page(
        selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)], disabled=disabled)], save_button=save
    )


def live_pages() -> dict[str, ParsedPage]:
    return {
        "services": services_page(rows=[CUSTOM_SSH, STALE]),
        "apphosting": apphosting_page(rows=[("custom_ssh", "host-b")], device_options=DEVICES),
        "ipalloc": ipalloc_page(),
        "dosprotect": default_dosprotect(),
        "packetfilter": page("packetfilter", buttons=[button("AddDropRule", "Add a 'Drop' Rule")]),
        "etherlan": page(
            "etherlan", fields=[hidden("nonce", "n"), field("lan_mtu", "text", "1500")], buttons=[button("Save")]
        ),
    }


DEVICES = (("aa:bb:cc:dd:ee:01", "host-b"), ("aa:bb:cc:dd:ee:02", "host-a"))


def dump() -> Snapshot:
    return Snapshot(
        meta=SnapshotMeta(schema=2, firmware="4.27.7", ts="t", router_host="r"),
        services=[
            SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
            SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
        ],
        forwards=[
            SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01"),
            SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"),
            SnapshotForward("Nope", "host-a", "aa:bb:cc:dd:ee:02"),
        ],
        reservations=[],
        forms={"dosprotect": {"flood_protect": "on"}},
        tables={"packetfilter": [{"button": "Add a 'Drop' Rule"}]},
    )


# A dump whose services are all already on the router, so the services page has no pending add.
# Remove planning is only unblocked on a page with no unblocked add step in the same plan
# (see the C1 tests below), so prune-focused tests start from this dump.
def dump_no_service_adds() -> Snapshot:
    base = dump()
    return replace(base, services=[s for s in base.services if s.name != "Mosh"])


def live_snapshot(pages: dict[str, ParsedPage]) -> Snapshot:
    return extract_snapshot(pages, ts="t", router_host="r")


def plan(snap: Snapshot, pages: dict[str, ParsedPage], options: RestoreOptions = OPTIONS) -> list[RestoreStep]:
    return build_restore_plan(
        diff_snapshots(snap, live_snapshot(pages), include_lan=options.include_lan), snap, pages, options
    )


def find(steps: list[RestoreStep], kind: str, contains: str | None = None) -> RestoreStep:
    for s in steps:
        if s.kind == kind and (contains is None or contains in s.description):
            return s
    raise AssertionError(f"no {kind} step{f' containing {contains!r}' if contains else ''}")


# --- Planning --------------------------------------------------------------------------------------


def test_restore_order_and_save_buttons_are_fixed():
    assert list(RESTORE_PAGE_ORDER) == [
        "services",
        "apphosting",
        "ipalloc",
        "packetfilter",
        "dosprotect",
        "wconfig",
        "etherlan",
    ]
    assert dict(FORM_SAVE_BUTTONS) == {"dosprotect": "Save", "wconfig": "Save", "etherlan": "Save"}


def test_build_restore_plan_adds_only_missing_services_and_forwards_in_order_with_add_payloads():
    steps = plan(dump(), live_pages())
    assert [f"{s.page}:{s.kind}" for s in steps] == [
        "services:add-service",
        "apphosting:add-forward",
        "apphosting:add-forward",
        "packetfilter:skip",
        "dosprotect:form",
    ]
    add_service = steps[0]
    assert add_service.button == "Add"
    assert add_service.service_name == "Mosh"
    assert add_service.raw_payload is not None
    assert (
        add_service.raw_payload.items()
        >= {
            "Service": "Mosh",
            "extMinPort": "60001",
            "extMaxPort": "60010",
            "intStartPort": "60001",
            "protocol": "UDP",
            "Add": "Add",
        }.items()
    )
    assert add_service.description == "add service Mosh UDP 60001-60010 -> 60001"
    assert [s.order for s in steps] == [1, 2, 3, 4, 5]
    assert steps[3].description == "packet filter rules are documentary only in v1; 1 rule rows in dump"


def test_build_restore_plan_refuses_dangerous_pages(monkeypatch):
    # The page order is fixed, so this can only be reached by tampering with it; the guard still
    # has to hold because a plan step for a dangerous page must never become postable.
    monkeypatch.setattr(restore, "RESTORE_PAGE_ORDER", (*RESTORE_PAGE_ORDER, "restart"))
    monkeypatch.setattr(restore, "compared_form_pages", lambda include_lan: ["dosprotect", "wconfig", "restart"])
    diff = SnapshotDiff(
        identical=False,
        services=EntryDiff(),
        forwards=EntryDiff(),
        reservations=ReservationDiff(),
        forms={"restart": [FormFieldDiff("x", "1", "2")]},
        firmware_changed=False,
    )
    pages = {"restart": page("restart", fields=[field("x", "text", "2")], buttons=[button("Save")])}
    with pytest.raises(RuntimeError, match="dangerous"):
        build_restore_plan(diff, dump(), pages, OPTIONS)


def test_forward_whose_service_is_not_in_the_dropdown_is_blocked_not_silently_dropped():
    steps = plan(dump(), live_pages())
    nope = find(steps, "add-forward", "Nope")
    assert nope.blocked is not None and "not offered" in nope.blocked
    mosh = find(steps, "add-forward", "Mosh")
    assert mosh.blocked is None
    assert mosh.raw_payload is not None
    assert mosh.raw_payload.items() >= {"service": "Mosh", "device": "aa:bb:cc:dd:ee:02"}.items()


def test_forward_for_an_unknown_device_is_blocked():
    snap = replace(dump_no_service_adds(), forwards=[SnapshotForward("Mosh", "ghost", "00:11:22:33:44:55")])
    step = find(plan(snap, live_pages()), "add-forward")
    assert step.blocked == "device 00:11:22:33:44:55 not in router device list"


def test_form_step_posts_only_differing_fields_with_the_page_save_button():
    form_step = find(plan(dump(), live_pages()), "form")
    assert form_step.page == "dosprotect"
    assert form_step.button == "Save"
    assert form_step.assignments == ["flood_protect=on"]
    assert form_step.raw_payload is not None
    assert form_step.raw_payload.items() >= {"flood_protect": "on", "Save": "Save"}.items()
    assert form_step.description == "save dosprotect: flood_protect: off -> on"


def test_prune_emits_remove_steps_for_extra_rows_and_never_without_prune():
    pages = live_pages()
    snap = dump_no_service_adds()
    assert not any(s.kind.startswith("remove") for s in plan(snap, pages))
    remove = find(plan(snap, pages, PRUNE), "remove-service")
    assert remove.button == "Remove_2"
    assert "Stale" in remove.description
    assert remove.raw_payload is not None and "Remove_2" in remove.raw_payload


def test_prune_emits_only_one_real_remove_per_page_and_blocks_the_rest():
    pages = {**live_pages(), "services": services_page(rows=[CUSTOM_SSH, STALE, ("Stale2", "2-2", "2", "TCP")])}
    removes = [s for s in plan(dump_no_service_adds(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    assert removes[0].blocked is None
    assert removes[0].button == "Remove_2"
    assert removes[1].blocked is not None and "separate --prune runs" in removes[1].blocked
    assert removes[1].raw_payload is None and removes[1].display_payload is None


def test_an_earlier_unresolvable_extra_does_not_block_a_later_genuinely_removable_extra():
    # Two rows share the name "Stale" (ambiguous -> blocked for both), and a third row "Stale2" has
    # a unique name and should become the real remove -- even though it is NOT the first extra in
    # the diff. Index-based limiting would have incorrectly stamped it "multiple removes" too.
    rows = [CUSTOM_SSH, STALE, ("Stale", "3-3", "3", "TCP"), ("Stale2", "2-2", "2", "TCP")]
    pages = {**live_pages(), "services": services_page(rows=rows)}
    removes = [s for s in plan(dump_no_service_adds(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 3
    assert "ambiguous" in (removes[0].blocked or "")
    assert "ambiguous" in (removes[1].blocked or "")
    assert removes[2].blocked is None
    assert removes[2].button == "Remove_4"
    assert "Stale2" in removes[2].description


def test_missing_live_page_blocks_its_steps_instead_of_throwing():
    pages = live_pages()
    del pages["dosprotect"]
    form_step = find(plan(dump(), pages), "form")
    assert form_step.blocked is not None and "not fetched" in form_step.blocked
    for name in ("services", "apphosting", "ipalloc"):
        del pages[name]
    snap = replace(dump(), reservations=[SnapshotReservation("02:0a:0b:0c:0d:04", "192.168.1.67")])
    steps = plan(snap, pages, PRUNE)
    assert {s.kind for s in steps} >= {"add-service", "add-forward", "reserve"}
    assert all("not fetched" in (s.blocked or "") for s in steps if s.kind != "skip")


def test_form_step_never_assigns_a_disabled_control_even_if_the_dump_differs():
    pages = {**live_pages(), "dosprotect": default_dosprotect(disabled=True)}
    assert not [s for s in plan(dump(), pages) if s.kind == "form" and s.page == "dosprotect"]


def test_form_step_skips_fields_absent_from_the_dump():
    snap = replace(dump(), forms={"dosprotect": {}})
    assert not [s for s in plan(snap, live_pages()) if s.kind == "form"]


# --- Fix round 1 -----------------------------------------------------------------------------------


def test_finding_1_a_missing_save_button_blocks_only_that_form_step_not_the_whole_plan():
    pages = {**live_pages(), "dosprotect": default_dosprotect(save="Apply")}
    steps = plan(dump(), pages)
    assert [f"{s.page}:{s.kind}" for s in steps] == [
        "services:add-service",
        "apphosting:add-forward",
        "apphosting:add-forward",
        "packetfilter:skip",
        "dosprotect:form",
    ]
    form_step = find(steps, "form")
    assert form_step.blocked is not None and "not found" in form_step.blocked
    assert form_step.raw_payload is None
    assert steps[0].blocked is None
    assert steps[3].blocked is None


def test_finding_2_an_unrelated_table_on_the_same_page_does_not_shift_remove_button_indices():
    port_status = [{"Port": "80", "Status": "open", "Notes": "-"}, {"Port": "443", "Status": "open", "Notes": "-"}]
    pages = {**live_pages(), "services": services_page(rows=[CUSTOM_SSH, STALE], extra_tables=port_status)}
    remove = find(plan(dump_no_service_adds(), pages, PRUNE), "remove-service")
    assert remove.blocked is None
    assert remove.button == "Remove_2"
    assert "Stale" in remove.description


def test_finding_2_a_row_button_count_mismatch_blocks_the_remove_instead_of_guessing():
    three_rows = services_page(
        rows=[CUSTOM_SSH, STALE, ("OrphanSvc", "9-9", "9", "TCP")], remove_buttons=["Remove_1", "Remove_2"]
    )
    removes = [s for s in plan(dump(), {**live_pages(), "services": three_rows}, PRUNE) if s.kind == "remove-service"]
    assert removes
    for step in removes:
        assert step.blocked is not None and "cannot map" in step.blocked
    assert "(3 rows, 2 buttons)" in (removes[0].blocked or "")


def test_finding_3_remove_matches_services_by_the_name_column_only_not_any_cell():
    pages = {**live_pages(), "services": services_page(rows=[CUSTOM_SSH, ("TCP", "7-7", "7", "UDP")])}
    remove = find(plan(dump_no_service_adds(), pages, PRUNE), "remove-service")
    # custom_ssh's Protocol cell is "TCP" too; a naive any-cell match would wrongly pick Remove_1.
    assert remove.blocked is None
    assert remove.button == "Remove_2"
    assert "TCP" in remove.description


def test_finding_3_duplicate_rows_sharing_a_name_block_the_remove_instead_of_guessing():
    pages = {
        **live_pages(),
        "services": services_page(rows=[CUSTOM_SSH, ("Dup", "5-5", "5", "TCP"), ("Dup", "6-6", "6", "TCP")]),
    }
    removes = [s for s in plan(dump(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    for step in removes:
        assert step.blocked is not None and "ambiguous" in step.blocked


def test_remove_of_a_row_that_vanished_is_blocked():
    pages = live_pages()
    live = live_snapshot(pages)
    stale_gone = {**pages, "services": services_page(rows=[CUSTOM_SSH])}
    diff = diff_snapshots(dump_no_service_adds(), live, include_lan=False)
    remove = find(build_restore_plan(diff, dump_no_service_adds(), stale_gone, PRUNE), "remove-service")
    assert remove.blocked == "no Remove button found for row 'Stale'"


def test_finding_4_form_step_description_reports_only_the_fields_actually_being_applied():
    pages = {
        **live_pages(),
        "dosprotect": dosprotect_page(
            fields=[field("log_attacks", "text", "no")],
            selects=[select("flood_protect", [("on", "On"), ("off", "Off", True)], disabled=True)],
        ),
    }
    custom = replace(dump(), forms={"dosprotect": {"flood_protect": "on", "log_attacks": "yes"}})
    form_step = find(plan(custom, pages), "form")
    assert form_step.assignments == ["log_attacks=yes"]
    assert "flood_protect" not in form_step.description
    assert "log_attacks" in form_step.description


def test_form_step_description_redacts_secrets_unless_include_secrets():
    wconfig = page("wconfig", fields=[field("key11", "text", "old")], buttons=[button("Save", "Save...")])
    pages = {**live_pages(), "wconfig": wconfig}
    snap = replace(dump_no_service_adds(), forwards=live_snapshot(pages).forwards, forms={"wconfig": {"key11": "new"}})
    hidden_step = find(plan(snap, pages), "form")
    assert hidden_step.description == "save wconfig: key11: [redacted] -> [redacted]"
    assert hidden_step.display_payload == {"key11": "[redacted]", "Save": "Save..."}
    assert hidden_step.raw_payload == {"key11": "new", "Save": "Save..."}
    shown = find(plan(snap, pages, replace(OPTIONS, include_secrets=True)), "form")
    assert shown.description == "save wconfig: key11: old -> new"


# --- Executor --------------------------------------------------------------------------------------


def post_response(status_code: int = 302, location: str | None = None):
    return SimpleNamespace(status_code=status_code, headers={"location": location} if location else {})


def get_response(status_code: int, body: str = ""):
    return SimpleNamespace(status_code=status_code, body=body)


class FakeClient:
    def __init__(self, post=None, get=None):
        self.posted: list[tuple[str, dict[str, str]]] = []
        self.gets: list[str] = []
        self._post = post or (lambda page, fields: post_response(302, f"/cgi-bin/{page}.ha"))
        self._get = get or (lambda page: (_ for _ in ()).throw(AssertionError("get_cgi_page should not be called")))

    def post_cgi_page(self, page, fields):
        self.posted.append((page, dict(fields)))
        return self._post(page, fields)

    def get_cgi_page(self, page):
        self.gets.append(page)
        return self._get(page)


@pytest.fixture
def fake_parser(monkeypatch):
    """Map GET body tokens to ParsedPage objects in place of parser.parse_page."""
    registry: dict[str, ParsedPage] = {}

    def parse(page: str, body: str) -> ParsedPage:
        return registry[body]

    monkeypatch.setattr(restore, "_parse_page", parse)
    return registry


def fake_steps() -> list[RestoreStep]:
    return [
        RestoreStep(1, "add-service", "services", "add A", button="Add", raw_payload={"Service": "A", "Add": "Add"}),
        RestoreStep(2, "add-forward", "apphosting", "blocked B", blocked="service 'B' not offered"),
        RestoreStep(3, "skip", "packetfilter", "skip"),
        RestoreStep(
            4, "form", "dosprotect", "save", button="Save", raw_payload={"flood_protect": "on", "Save": "Save"}
        ),
        RestoreStep(5, "form", "wconfig", "save wifi", button="Save", raw_payload={"x": "1"}),
    ]


def test_execute_restore_posts_only_runnable_steps_in_order_and_records_statuses():
    client = FakeClient()
    seen: list[str] = []
    execution = execute_restore(client, list(reversed(fake_steps())), lambda r: seen.append(f"{r.order}:{r.status}"))
    assert [p for p, _ in client.posted] == ["services", "dosprotect", "wconfig"]
    assert [s.status for s in execution.steps] == ["applied", "blocked", "skipped", "applied", "applied"]
    assert execution.steps[0].location == "/cgi-bin/services.ha"
    assert execution.steps[0].status_code == 302
    assert execution.steps[1].error == "service 'B' not offered"
    assert execution.stopped_at is None
    assert seen == ["1:applied", "2:blocked", "3:skipped", "4:applied", "5:applied"]


def test_execute_restore_treats_a_step_without_payload_as_blocked():
    execution = execute_restore(FakeClient(), [RestoreStep(1, "form", "dosprotect", "save", button="Save")])
    assert execution.steps[0].status == "blocked"
    assert execution.steps[0].error == "no payload"


def test_execute_restore_stops_at_the_first_failure_and_marks_the_rest_not_run():
    calls = {"n": 0}

    def post(page, fields):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Router rejected dosprotect.ha with HTTP 500.")
        return post_response(200)

    execution = execute_restore(FakeClient(post=post), fake_steps())
    assert [s.status for s in execution.steps] == ["applied", "blocked", "skipped", "failed", "not-run"]
    assert execution.stopped_at == 4
    assert "HTTP 500" in (execution.steps[3].error or "")
    assert calls["n"] == 2


def test_execute_restore_fails_a_step_on_a_non_2xx_3xx_status():
    execution = execute_restore(FakeClient(post=lambda p, f: post_response(500)), fake_steps()[:1])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].status_code == 500
    assert execution.steps[0].error == "unexpected HTTP 500"
    assert execution.stopped_at == 1


def test_execute_restore_reads_location_case_insensitively_and_from_lists():
    client = FakeClient(
        post=lambda p, f: SimpleNamespace(status_code=302, headers={"Location": ["/cgi-bin/a.ha", "x"]})
    )
    execution = execute_restore(client, fake_steps()[:1])
    assert execution.steps[0].location == "/cgi-bin/a.ha"


def convergence_diff(**overrides) -> SnapshotDiff:
    base = SnapshotDiff(
        identical=False,
        services=EntryDiff(),
        forwards=EntryDiff(),
        reservations=ReservationDiff(),
        forms={},
        firmware_changed=False,
    )
    return replace(base, **overrides)


STALE_SERVICE = SnapshotService("Stale", 1, 1, 1, "TCP")
STALE_FORWARD = SnapshotForward("Stale", "host-a", "aa:bb:cc:dd:ee:02")


def test_restore_converged_ignores_router_extras_when_prune_was_not_given():
    diff = convergence_diff(services=EntryDiff(extra=[STALE_SERVICE]), forwards=EntryDiff(extra=[STALE_FORWARD]))
    assert restore_converged(diff, False) is True


def test_restore_converged_still_reports_router_extras_as_unconverged_with_prune():
    assert restore_converged(convergence_diff(services=EntryDiff(extra=[STALE_SERVICE])), True) is False
    assert restore_converged(convergence_diff(forwards=EntryDiff(extra=[STALE_FORWARD])), True) is False


def test_restore_converged_is_false_while_anything_from_the_dump_is_still_missing():
    assert restore_converged(convergence_diff(services=EntryDiff(missing=[STALE_SERVICE])), False) is False
    assert restore_converged(convergence_diff(forwards=EntryDiff(missing=[STALE_FORWARD])), False) is False


def test_restore_converged_is_false_while_a_compared_form_still_differs():
    diff = convergence_diff(forms={"dosprotect": [FormFieldDiff("flood_protect", "on", "off")]})
    assert restore_converged(diff, False) is False
    assert restore_converged(diff, True) is False


def test_restore_converged_is_true_for_an_identical_post_restore_diff():
    assert restore_converged(convergence_diff(identical=True), False) is True
    assert restore_converged(convergence_diff(identical=True), True) is True


# --- Fix round 2 -----------------------------------------------------------------------------------


def test_c1_a_services_page_with_a_pending_add_blocks_its_removes_in_the_same_run():
    # The dump is missing Mosh (an add on `services`) and the router has an extra `Stale` row.
    # Running the add first can re-sort the table, so Remove_<n> resolved at plan time is no
    # longer trustworthy: the remove must be deferred to a separate --prune run.
    steps = plan(dump(), live_pages(), PRUNE)
    assert find(steps, "add-service").blocked is None
    remove = find(steps, "remove-service")
    assert remove.blocked is not None and "pending adds" in remove.blocked
    assert remove.raw_payload is None
    assert remove.display_payload is None


def test_c1_with_no_add_pending_on_the_page_the_remove_stays_unblocked():
    steps = plan(dump_no_service_adds(), live_pages(), PRUNE)
    assert not any(s.kind == "add-service" for s in steps)
    remove = find(steps, "remove-service")
    assert remove.blocked is None
    assert remove.button == "Remove_2"


def apphosting_extra() -> ParsedPage:
    return apphosting_page(rows=[("custom_ssh", "host-b"), ("Mosh", "host-b")], device_options=DEVICES)


def test_c1_an_apphosting_page_with_a_pending_add_blocks_its_forward_removes_too():
    # Dump wants Mosh -> host-a; the router has Mosh -> host-b. That is one add and one extra on the
    # same page, so the remove has to wait for a separate --prune run.
    steps = plan(dump(), {**live_pages(), "apphosting": apphosting_extra()}, PRUNE)
    assert find(steps, "add-forward", "Mosh").blocked is None
    remove = find(steps, "remove-forward")
    assert remove.blocked is not None and "pending adds" in remove.blocked


def test_c1_an_apphosting_page_with_no_pending_add_still_removes_the_extra_forward():
    snap = replace(dump(), forwards=[SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01")])
    steps = plan(snap, {**live_pages(), "apphosting": apphosting_extra()}, PRUNE)
    assert not any(s.kind == "add-forward" for s in steps)
    remove = find(steps, "remove-forward")
    assert remove.blocked is None
    assert remove.button == "Remove_2"
    assert remove.description == "remove forward Mosh -> host-b"


def test_remove_forward_matches_on_both_service_and_device_label():
    two_host_b = apphosting_page(rows=[("Mosh", "host-a"), ("Mosh", "host-b")], device_options=DEVICES)
    snap = replace(dump(), forwards=[SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")])
    remove = find(plan(snap, {**live_pages(), "apphosting": two_host_b}, PRUNE), "remove-forward")
    assert remove.blocked is None
    assert remove.button == "Remove_2"


def test_c1_a_blocked_for_pending_adds_remove_does_not_consume_the_one_remove_per_page_slot():
    pages = {**live_pages(), "services": services_page(rows=[CUSTOM_SSH, STALE, ("Stale2", "2-2", "2", "TCP")])}
    removes = [s for s in plan(dump(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    for step in removes:
        assert step.blocked is not None and "pending adds" in step.blocked


def test_i7_the_etherlan_form_is_not_planned_without_include_lan():
    snap = replace(dump(), forms={**dump().forms, "etherlan": {"lan_mtu": "1400"}})
    assert not any(s.page == "etherlan" for s in plan(snap, live_pages()))


def test_i7_include_lan_plans_exactly_one_etherlan_form_save():
    snap = replace(dump(), forms={**dump().forms, "etherlan": {"lan_mtu": "1400"}})
    lan_steps = [s for s in plan(snap, live_pages(), replace(OPTIONS, include_lan=True)) if s.page == "etherlan"]
    assert len(lan_steps) == 1
    assert lan_steps[0].kind == "form"
    assert lan_steps[0].button == "Save"
    assert lan_steps[0].assignments == ["lan_mtu=1400"]
    assert lan_steps[0].raw_payload is not None
    assert lan_steps[0].raw_payload.items() >= {"lan_mtu": "1400", "Save": "Save"}.items()


def test_add_service_resolves_the_protocol_against_the_live_selects_option_values():
    real_select = [("both", "TCP/UDP"), ("tcp", "TCP"), ("udp", "UDP")]
    pages = {**live_pages(), "services": services_page(rows=[CUSTOM_SSH, STALE], protocol_options=real_select)}
    wanted = replace(
        dump(),
        services=[
            SnapshotService("custom_ssh", 2483, 2483, 22, "TCP"),
            SnapshotService("Mosh", 60001, 60010, 60001, "UDP"),
            SnapshotService("Both1", 7000, 7000, 7000, "TCP/UDP"),
            SnapshotService("Weird", 7001, 7001, 7001, "SCTP"),
        ],
        forwards=[],
    )
    steps = [s for s in plan(wanted, pages) if s.kind == "add-service"]
    assert [s.description.split(" ")[2] for s in steps] == ["Mosh", "Both1", "Weird"]
    assert (
        steps[0].raw_payload is not None
        and steps[0].raw_payload.items() >= {"Service": "Mosh", "protocol": "udp"}.items()
    )
    assert (
        steps[1].raw_payload is not None
        and steps[1].raw_payload.items() >= {"Service": "Both1", "protocol": "both"}.items()
    )
    assert steps[2].blocked is not None and "protocol 'SCTP' not offered" in steps[2].blocked


def test_reserve_steps_are_planned_for_missing_and_changed_reservations_after_forwards_before_packetfilter():
    pages = live_pages()
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(),
        forwards=live.forwards,
        reservations=[
            SnapshotReservation("02:0a:0b:0c:0d:04", "192.168.1.67"),
            SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
            SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.71"),
        ],
    )
    steps = plan(wanted, pages)
    kinds = [f"{s.page}:{s.kind}" for s in steps]
    assert kinds.index("ipalloc:reserve") < kinds.index("packetfilter:skip")
    reserves = [s for s in steps if s.kind == "reserve"]
    assert [s.description for s in reserves] == [
        "reserve 192.168.1.67 for 02:0a:0b:0c:0d:04 (currently DHCP)",
        "reserve 192.168.1.71 for 02:0a:0b:0c:0d:03 (currently 192.168.1.70)",
    ]
    assert reserves[0].button == "Allocate_02:0a:0b:0c:0d:04"
    assert reserves[0].raw_payload == {"Allocate_02:0a:0b:0c:0d:04": "Allocate"}
    assert reserves[0].follow_up == RestoreFollowUp("ipalloc", "alloc_02:0a:0b:0c:0d:04", "192.168.1.67", "Save")
    assert all(s.blocked is None for s in reserves)


def test_follow_up_payload_falls_back_to_the_button_name_until_the_live_value_is_known():
    follow_up = RestoreFollowUp("ipalloc", "alloc_aa", "192.168.1.5", "Save")
    assert follow_up_payload(follow_up) == {"alloc_aa": "192.168.1.5", "Save": "Save"}
    assert follow_up_payload(follow_up, "Save...") == {"alloc_aa": "192.168.1.5", "Save": "Save..."}


def test_a_reservation_for_a_device_not_on_the_ip_allocation_page_is_blocked():
    pages = live_pages()
    wanted = replace(
        dump_no_service_adds(),
        forwards=live_snapshot(pages).forwards,
        reservations=[SnapshotReservation("00:11:22:33:44:55", "192.168.1.80")],
    )
    reserve = find(plan(wanted, pages), "reserve")
    assert reserve.blocked is not None and "not present on IP Allocation page" in reserve.blocked
    assert reserve.raw_payload is None


def test_extra_reservations_are_never_released_even_with_prune():
    pages = live_pages()
    wanted = replace(dump_no_service_adds(), forwards=live_snapshot(pages).forwards, reservations=[])
    assert [s for s in plan(wanted, pages, PRUNE) if s.page == "ipalloc"] == []


def test_restore_converged_treats_missing_or_changed_reservations_as_unconverged_and_ignores_extras():
    r = SnapshotReservation("a", "1.1.1.1")
    assert restore_converged(convergence_diff(reservations=ReservationDiff(missing=[r])), False) is False
    changed = ReservationDiff(changed=[ReservationChange("a", "1.1.1.1", "1.1.1.2")])
    assert restore_converged(convergence_diff(reservations=changed), False) is False
    assert restore_converged(convergence_diff(reservations=ReservationDiff(extra=[r])), True) is True


def test_wconfig_form_step_uses_the_save_button():
    wconfig = page(
        "wconfig",
        fields=[hidden("nonce", "n"), field("maxclients", "text", "80")],
        buttons=[button("Update"), button("Save", "Save...")],
    )
    pages = {**live_pages(), "wconfig": wconfig}
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(), forwards=live.forwards, forms={**live.forms, "wconfig": {"maxclients": "81"}}
    )
    form_step = find(plan(wanted, pages), "form", "wconfig")
    assert form_step.button == "Save"
    assert form_step.raw_payload is not None
    assert form_step.raw_payload.items() >= {"maxclients": "81", "Save": "Save..."}.items()


# --- Task 5: Executor follow-up (Allocate -> GET -> Save) ---------------------------------------------

MAC = "02:0a:0b:0c:0d:04"


def entry_page(mac: str, ips: list[str], save: str | None = "Save", disabled_ips: list[str] = ()) -> ParsedPage:
    options = [("normal", "Address from DHCP pool", True), *[(ip, f"Private fixed:{ip}") for ip in ips]]
    buttons = ([button("Save", save)] if save is not None else []) + [button("Cancel")]
    return page(
        "ipalloc",
        fields=[hidden("nonce", "n")],
        selects=[select(f"alloc_{mac}", options, disabled_options=disabled_ips)],
        buttons=buttons,
        forms=[form("/cgi-bin/ipalloc.ha")],
    )


def reserve_fake(mac: str, ip: str) -> RestoreStep:
    return RestoreStep(
        1,
        "reserve",
        "ipalloc",
        f"reserve {ip} for {mac} (currently DHCP)",
        button=f"Allocate_{mac}",
        raw_payload={f"Allocate_{mac}": "Allocate"},
        follow_up=RestoreFollowUp("ipalloc", f"alloc_{mac}", ip, "Save"),
    )


def test_execute_restore_performs_allocate_reads_the_entry_form_then_posts_save(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67", "192.168.1.68"])
    client = FakeClient(
        post=lambda p, f: post_response(302, "/cgi-bin/ipalloc.ha"), get=lambda p: get_response(200, "entry")
    )
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert client.gets == ["ipalloc"]
    assert client.posted == [
        ("ipalloc", {f"Allocate_{MAC}": "Allocate"}),
        ("ipalloc", {f"alloc_{MAC}": "192.168.1.67", "Save": "Save"}),
    ]
    assert execution.steps[0].status == "applied"
    assert execution.steps[0].location == "/cgi-bin/ipalloc.ha"
    assert execution.stopped_at is None


def test_execute_restore_fails_the_reserve_step_without_posting_save_when_the_address_is_not_offered(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.68"])
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    skip = RestoreStep(2, "skip", "packetfilter", "skip")
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67"), skip])
    assert [list(f) for _, f in client.posted] == [[f"Allocate_{MAC}"]]
    assert execution.steps[0].status == "failed"
    assert "not offered" in (execution.steps[0].error or "")
    assert execution.stopped_at == 1
    assert execution.steps[1].status == "not-run"


def test_execute_restore_fails_the_reserve_step_when_the_entry_form_belongs_to_a_different_device(fake_parser):
    fake_parser["entry"] = entry_page("aa:bb:cc:dd:ee:ff", ["192.168.1.67"])
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == f"IP Allocation Entry did not open for {MAC}"


def test_execute_restore_fails_the_reserve_step_when_the_follow_up_save_returns_a_bad_status(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67", "192.168.1.68"])
    calls = {"n": 0}

    def post(page, fields):
        calls["n"] += 1
        return post_response(302) if calls["n"] == 1 else post_response(500)

    execution = execute_restore(
        FakeClient(post=post, get=lambda p: get_response(200, "entry")), [reserve_fake(MAC, "192.168.1.67")]
    )
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].status_code == 500
    assert "HTTP 500" in (execution.steps[0].error or "")
    assert execution.stopped_at == 1


def test_execute_restore_fails_the_reserve_step_when_get_cgi_page_raises_without_posting_save():
    def get(page):
        raise RuntimeError("socket hang up")

    client = FakeClient(post=lambda p, f: post_response(302), get=get)
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "failed"
    assert "socket hang up" in (execution.steps[0].error or "")
    assert execution.stopped_at == 1
    assert [list(f) for _, f in client.posted] == [[f"Allocate_{MAC}"]]


def test_execute_restore_fails_the_reserve_step_when_the_follow_up_get_returns_non_200():
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(302, ""))
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "unexpected HTTP 302 reading ipalloc"
    assert execution.stopped_at == 1


def test_execute_restore_refuses_to_post_an_allocation_value_that_is_not_a_dotted_ipv4_address(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67"])
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    execution = execute_restore(client, [reserve_fake(MAC, "normal")])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "refusing to post non-IPv4 allocation value 'normal'"
    assert [list(f) for _, f in client.posted] == [[f"Allocate_{MAC}"]]
    assert execution.stopped_at == 1


# The gateway keeps an "IP Allocation Entry" block rendered for the rest of the web session after
# an Allocate/Cancel, so the base ipalloc page can carry a stale `alloc_<other mac>` select whose
# selected option is `normal` ("Address from DHCP pool").
def test_a_reserve_step_posts_only_the_allocate_button_never_a_sticky_entry_blocks_alloc_select():
    sticky = ipalloc_page(
        extra_selects=[
            select(
                "alloc_aa:bb:cc:dd:ee:ff",
                [("normal", "Address from DHCP pool", True), ("192.168.1.68", "Private fixed:192.168.1.68")],
            )
        ],
        extra_buttons=[button("Save")],
    )
    pages = {**live_pages(), "ipalloc": sticky}
    wanted = replace(
        dump_no_service_adds(),
        forwards=live_snapshot(pages).forwards,
        reservations=[SnapshotReservation(MAC, "192.168.1.67")],
    )
    reserve = find(plan(wanted, pages), "reserve")
    assert reserve.blocked is None
    assert reserve.raw_payload == {f"Allocate_{MAC}": "Allocate"}
    assert reserve.display_payload == {f"Allocate_{MAC}": "Allocate"}
    assert not any(name.lower().startswith("alloc_") for name in reserve.raw_payload)


# This gateway drops POSTs whose submit value does not match the rendered control, so the Save
# POST has to carry the value the entry page actually renders ("Save..." on some firmwares).
def test_execute_restore_posts_the_save_buttons_rendered_value_not_its_name(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67"], save="Save...")
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "applied"
    assert client.posted[1] == ("ipalloc", {f"alloc_{MAC}": "192.168.1.67", "Save": "Save..."})


def test_execute_restore_fails_the_reserve_step_when_the_entry_form_has_no_save_button(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67"], save=None)
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "IP Allocation Entry has no 'Save' button"
    assert [list(f) for _, f in client.posted] == [[f"Allocate_{MAC}"]]
    assert execution.stopped_at == 1


def test_execute_restore_fails_the_reserve_step_when_the_matching_address_option_is_disabled(fake_parser):
    fake_parser["entry"] = entry_page(MAC, ["192.168.1.67"], disabled_ips=["192.168.1.67"])
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "entry"))
    execution = execute_restore(client, [reserve_fake(MAC, "192.168.1.67")])
    assert execution.steps[0].status == "failed"
    assert "not offered" in (execution.steps[0].error or "")
    assert [list(f) for _, f in client.posted] == [[f"Allocate_{MAC}"]]
    assert execution.stopped_at == 1


def test_add_forward_matches_a_custom_service_that_the_router_lists_with_a_star_prefix():
    starred = apphosting_page(
        rows=[("custom_ssh", "host-b")], service_options=["custom_ssh", "*Mosh", "SSH server"], device_options=DEVICES
    )
    pages = {**live_pages(), "apphosting": starred}
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(), forwards=[*live.forwards, SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")]
    )
    add = find(plan(wanted, pages), "add-forward")
    assert add.blocked is None
    assert add.raw_payload is not None
    assert add.raw_payload.items() >= {"service": "*Mosh", "device": "aa:bb:cc:dd:ee:02"}.items()


def test_prune_removes_a_forward_before_the_custom_service_it_references():
    pages = {
        **live_pages(),
        "services": services_page(rows=[CUSTOM_SSH, ("zz_test", "61000-61000", "61000", "TCP")]),
        "apphosting": apphosting_page(rows=[("custom_ssh", "host-b"), ("zz_test", "host-a")], device_options=DEVICES),
    }
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(),
        services=[s for s in live.services if s.name != "zz_test"],
        forwards=[f for f in live.forwards if f.service != "zz_test"],
    )
    steps = plan(wanted, pages, PRUNE)
    remove_forward = find(steps, "remove-forward")
    remove_service = find(steps, "remove-service")
    assert remove_forward.blocked is None
    assert remove_service.blocked is None
    assert remove_forward.order < remove_service.order


# Real page shape (2026-09-19): Continue posts back to wconfig.ha, Cancel posts to the warning page.
def wifi_warn_page() -> ParsedPage:
    return page(
        "wifiwarn_advanced",
        title="Wi-Fi Warning",
        fields=[hidden("nonce", "n")],
        buttons=[button("Continue"), button("Cancel")],
        forms=[
            form("/cgi-bin/wconfig.ha", fields=["nonce"], buttons=["Continue"]),
            form("/cgi-bin/wifiwarn_advanced.ha", fields=["nonce"], buttons=["Cancel"]),
        ],
    )


WCONFIG_SAVE = RestoreStep(
    1, "form", "wconfig", "save wconfig", button="Save", raw_payload={"maxclients": "81", "Save": "Save..."}
)


def test_a_form_save_that_redirects_to_the_wifi_warning_page_is_confirmed_with_continue(fake_parser):
    fake_parser["warn"] = wifi_warn_page()
    fake_parser["blank"] = page("blank")

    def post(page, fields):
        return post_response(302, "/cgi-bin/wconfig.ha" if "Continue" in fields else "/cgi-bin/wifiwarn_advanced.ha")

    client = FakeClient(post=post, get=lambda p: get_response(200, "warn" if p == "wifiwarn_advanced" else "blank"))
    execution = execute_restore(client, [WCONFIG_SAVE])
    assert client.gets == ["wifiwarn_advanced"]
    assert client.posted == [
        ("wconfig", {"maxclients": "81", "Save": "Save..."}),
        ("wconfig", {"Continue": "Continue"}),
    ]
    assert execution.steps[0].status == "applied"
    assert execution.steps[0].location == "/cgi-bin/wconfig.ha"


def test_continue_falls_back_to_the_warning_page_when_no_form_owns_the_button(fake_parser):
    fake_parser["warn"] = page("wifiwarn_advanced", buttons=[button("Continue", "Go on")])
    client = FakeClient(
        post=lambda p, f: post_response(302, "/cgi-bin/wifiwarn_advanced.ha" if "Continue" not in f else None),
        get=lambda p: get_response(200, "warn"),
    )
    execution = execute_restore(client, [WCONFIG_SAVE])
    assert client.posted[1] == ("wifiwarn_advanced", {"Continue": "Go on"})
    assert execution.steps[0].status == "applied"


def test_a_wifi_warning_page_without_a_continue_button_fails_the_step_without_a_second_post(fake_parser):
    fake_parser["warn"] = page(
        "wifiwarn_advanced",
        buttons=[button("Cancel")],
        forms=[form("/cgi-bin/wifiwarn_advanced.ha", buttons=["Cancel"])],
    )
    client = FakeClient(
        post=lambda p, f: post_response(302, "/cgi-bin/wifiwarn_advanced.ha"), get=lambda p: get_response(200, "warn")
    )
    execution = execute_restore(
        client, [RestoreStep(1, "form", "wconfig", "save", button="Save", raw_payload={"x": "1"})]
    )
    assert [p for p, _ in client.posted] == ["wconfig"]
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "wifiwarn_advanced has no Continue button; change not confirmed"
    assert execution.stopped_at == 1


def test_a_disabled_continue_button_does_not_count(fake_parser):
    fake_parser["warn"] = page("wifiwarn_advanced", buttons=[button("Continue", disabled=True)])
    client = FakeClient(
        post=lambda p, f: post_response(302, "/cgi-bin/wifiwarn_advanced.ha"), get=lambda p: get_response(200, "warn")
    )
    execution = execute_restore(client, [WCONFIG_SAVE])
    assert execution.steps[0].status == "failed"
    assert "no Continue button" in (execution.steps[0].error or "")


def test_a_warning_page_that_fails_to_load_or_confirm_fails_the_step(fake_parser):
    fake_parser["warn"] = wifi_warn_page()
    client = FakeClient(
        post=lambda p, f: post_response(302, "/cgi-bin/wifiwarn_advanced.ha"), get=lambda p: get_response(500, "")
    )
    execution = execute_restore(client, [WCONFIG_SAVE])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "unexpected HTTP 500 reading wifiwarn_advanced"

    def post(page, fields):
        return post_response(302, "/cgi-bin/wifiwarn_advanced.ha") if "Continue" not in fields else post_response(500)

    client = FakeClient(post=post, get=lambda p: get_response(200, "warn"))
    execution = execute_restore(client, [WCONFIG_SAVE])
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].error == "unexpected HTTP 500 confirming wconfig"
    assert execution.stopped_at == 1


# --- Deferred forwards: service added in the same run --------------------------------------------
# The NAT/Gaming dropdown only lists a custom service after it exists. When the plan adds the
# service itself, its forward must not be blocked for a second invocation: the executor re-reads
# the dropdown after the services-page adds and resolves the forward then.


def apphosting_without_mosh() -> ParsedPage:
    return apphosting_page(rows=[("custom_ssh", "host-b")], service_options=["custom_ssh"], device_options=DEVICES)


def apphosting_with_star_mosh() -> ParsedPage:
    return apphosting_page(
        rows=[("custom_ssh", "host-b")], service_options=["custom_ssh", "*Mosh"], device_options=DEVICES
    )


def pages_without_mosh() -> dict[str, ParsedPage]:
    return {**live_pages(), "apphosting": apphosting_without_mosh()}


def test_forward_for_a_service_added_earlier_in_the_same_plan_is_deferred_not_blocked():
    steps = plan(dump(), pages_without_mosh())
    assert find(steps, "add-service").blocked is None
    mosh = find(steps, "add-forward", "Mosh")
    assert mosh.blocked is None
    assert mosh.raw_payload is None
    assert mosh.deferred == RestoreDeferredForward(
        SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"),
        "service 'Mosh' is added earlier in this run; the NAT/Gaming dropdown is re-read after that add",
    )
    # A service the plan does NOT add stays blocked exactly as before.
    nope = find(steps, "add-forward", "Nope")
    assert nope.blocked is not None and "not offered" in nope.blocked
    assert nope.deferred is None


def test_a_forward_whose_service_add_is_itself_blocked_stays_blocked_not_deferred():
    pages = {**pages_without_mosh(), "services": services_page(rows=[CUSTOM_SSH, STALE], protocol_options=["TCP"])}
    steps = plan(dump(), pages)
    assert "not offered" in (find(steps, "add-service").blocked or "")
    mosh = find(steps, "add-forward", "Mosh")
    assert mosh.deferred is None
    assert "not offered" in (mosh.blocked or "")


def test_a_deferred_forward_counts_as_a_pending_add_so_apphosting_removes_stay_blocked_under_prune():
    wanted = replace(dump(), forwards=[f for f in dump().forwards if f.service != "custom_ssh"])
    remove = find(plan(wanted, pages_without_mosh(), PRUNE), "remove-forward")
    assert remove.blocked is not None and "pending adds" in remove.blocked


def deferred_steps() -> list[RestoreStep]:
    return [
        RestoreStep(
            1,
            "add-service",
            "services",
            "add service Mosh",
            button="Add",
            raw_payload={"Service": "Mosh", "Add": "Add"},
        ),
        RestoreStep(
            2,
            "add-forward",
            "apphosting",
            "add forward Mosh -> host-a",
            deferred=RestoreDeferredForward(
                SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"), "service 'Mosh' is added earlier in this run"
            ),
        ),
        RestoreStep(
            3, "form", "dosprotect", "save", button="Save", raw_payload={"flood_protect": "on", "Save": "Save"}
        ),
    ]


def test_execute_restore_re_reads_the_dropdown_and_posts_a_deferred_forward_once_its_service_exists(fake_parser):
    fake_parser["apphosting"] = apphosting_with_star_mosh()
    client = FakeClient(get=lambda p: get_response(200, "apphosting"))
    execution = execute_restore(client, deferred_steps())
    assert client.gets == ["apphosting"]
    assert [p for p, _ in client.posted] == ["services", "apphosting", "dosprotect"]
    assert client.posted[1][1].items() >= {"service": "*Mosh", "device": "aa:bb:cc:dd:ee:02", "Add": "Add"}.items()
    assert [s.status for s in execution.steps] == ["applied", "applied", "applied"]
    assert execution.steps[1].location == "/cgi-bin/apphosting.ha"
    assert execution.stopped_at is None


def test_execute_restore_leaves_a_deferred_forward_blocked_when_the_dropdown_still_lacks_its_service(fake_parser):
    fake_parser["apphosting"] = apphosting_without_mosh()
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(200, "apphosting"))
    execution = execute_restore(client, deferred_steps())
    assert [p for p, _ in client.posted] == ["services", "dosprotect"]
    assert [s.status for s in execution.steps] == ["applied", "blocked", "applied"]
    assert "not offered" in (execution.steps[1].error or "")
    assert execution.stopped_at is None


def test_execute_restore_records_a_failed_deferred_forward_when_re_reading_apphosting_throws():
    def get(page):
        raise RuntimeError("Timed out reading apphosting.ha")

    client = FakeClient(post=lambda p, f: post_response(302), get=get)
    execution = execute_restore(client, deferred_steps())
    assert [p for p, _ in client.posted] == ["services"]
    assert [s.status for s in execution.steps] == ["applied", "failed", "not-run"]
    assert "Timed out" in (execution.steps[1].error or "")
    assert execution.stopped_at == 2


def test_execute_restore_fails_a_deferred_forward_on_a_non_200_re_read_or_rejected_post(fake_parser):
    client = FakeClient(post=lambda p, f: post_response(302), get=lambda p: get_response(503, ""))
    execution = execute_restore(client, deferred_steps())
    assert execution.steps[1].status == "failed"
    assert execution.steps[1].error == "unexpected HTTP 503 re-reading apphosting"
    fake_parser["apphosting"] = apphosting_with_star_mosh()
    client = FakeClient(
        post=lambda p, f: post_response(500 if p == "apphosting" else 302),
        get=lambda p: get_response(200, "apphosting"),
    )
    execution = execute_restore(client, deferred_steps())
    assert execution.steps[1].status == "failed"
    assert execution.steps[1].status_code == 500
    assert execution.stopped_at == 2


# --- Unchecked controls ------------------------------------------------------------------------------


def checkbox_pages() -> dict[str, ParsedPage]:
    return {
        **live_pages(),
        "dosprotect": dosprotect_page(fields=[checkbox("reflexive", "on", checked=True), checkbox("algsip", "on")]),
    }


def test_a_box_unchecked_in_the_dump_but_checked_live_is_restored_by_omitting_it_from_the_save_post():
    pages = checkbox_pages()
    live = live_snapshot(pages)
    assert live.forms["dosprotect"] == {"reflexive": "on", "algsip": UNCHECKED}
    wanted = replace(
        dump_no_service_adds(),
        forwards=live.forwards,
        forms={"dosprotect": {"reflexive": UNCHECKED, "algsip": UNCHECKED}},
    )
    form_step = find(plan(wanted, pages), "form", "dosprotect")
    assert form_step.blocked is None
    assert form_step.raw_payload == {"Save": "Save"}
    assert form_step.display_payload is not None and "reflexive" not in form_step.display_payload
    assert "reflexive: on -> <unchecked>" in form_step.description
    assert form_step.assignments == []


def test_a_box_checked_in_the_dump_but_unchecked_live_is_restored_by_posting_its_value():
    pages = checkbox_pages()
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(), forwards=live.forwards, forms={"dosprotect": {"reflexive": "on", "algsip": "on"}}
    )
    form_step = find(plan(wanted, pages), "form", "dosprotect")
    assert form_step.raw_payload == {"reflexive": "on", "algsip": "on", "Save": "Save"}


def test_identical_unchecked_state_on_both_sides_is_not_a_difference():
    pages = checkbox_pages()
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(), forwards=live.forwards, forms={"dosprotect": {"reflexive": "on", "algsip": UNCHECKED}}
    )
    assert diff_snapshots(wanted, live, include_lan=False).forms == {}


def test_a_text_field_whose_dump_value_is_literally_unchecked_is_assigned_not_omitted():
    pages = {
        **live_pages(),
        "dosprotect": dosprotect_page(
            fields=[field("display_label", "text", "Living room"), checkbox("reflexive", "on", checked=True)]
        ),
    }
    live = live_snapshot(pages)
    wanted = replace(
        dump_no_service_adds(),
        forwards=live.forwards,
        forms={"dosprotect": {"display_label": UNCHECKED, "reflexive": UNCHECKED}},
    )
    form_step = find(plan(wanted, pages), "form", "dosprotect")
    # The sentinel only means "off" for a checkable control; for the text input it is the literal value.
    assert form_step.raw_payload == {"display_label": UNCHECKED, "Save": "Save"}
