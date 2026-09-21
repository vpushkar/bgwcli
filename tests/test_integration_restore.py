"""Integration: real parser -> snapshot -> diff -> restore plan -> execute.

Re-derives every tests/restore.test.ts case that fed inline HTML through parsePage (planning against
live pages, and the executor's follow-up GETs which parse router HTML), now through
bgwcli.parser.parse_page and the real snapshot/diff/restore modules. The executor is driven with a
fake poster that returns router HTML; restore._parse_page is NOT monkeypatched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from integration_html import (
    APPHOSTING_EXTRA_HTML,
    APPHOSTING_WITH_STAR_MOSH_HTML,
    APPHOSTING_WITHOUT_MOSH_HTML,
    DISABLED_DOSPROTECT_HTML,
    DOSPROTECT_CHECKBOX_HTML,
    DOSPROTECT_RENAMED_BUTTON_HTML,
    DUP_NAME_HTML,
    ETHERLAN_HTML,
    IPALLOC_HTML,
    IPALLOC_STICKY_HTML,
    PACKETFILTER_HTML,
    PORT_STATUS_TABLE,
    REAL_PROTOCOL_SELECT,
    RESTORE_APPHOSTING_HTML,
    RESTORE_DOSPROTECT_HTML,
    RESTORE_SERVICES_HTML,
    STALE2_REMOVE4_ROW,
    STALE2_ROW,
    STALE_3_3_REMOVE3_ROW,
    STALE_ROW,
    TCP_NAMED_SERVICE_HTML,
    TEXT_FIELD_LITERAL_UNCHECKED_HTML,
    TWO_FIELD_DOSPROTECT_HTML,
    WCONFIG_SAVE_DOTS_HTML,
    WIFI_WARN_HTML,
    WIFI_WARN_NO_CONTINUE_HTML,
    ZZ_TEST_SERVICE_ROW,
    entry_page_html,
    entry_page_html_disabled_option,
    entry_page_html_with_save,
)

from bgwcli.parser import parse_page
from bgwcli.restore import (
    FORM_SAVE_BUTTONS,
    RESTORE_PAGE_ORDER,
    RestoreDeferredForward,
    RestoreFollowUp,
    RestoreOptions,
    RestoreStep,
    build_restore_plan,
    execute_restore,
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
from bgwcli.snapshot_diff import diff_snapshots
from bgwcli.types import ParsedPage

META = SnapshotMeta(firmware="4.27.7", ts="t", router_host="r")
OPTIONS = RestoreOptions(prune=False, include_secrets=False)
PRUNE = replace(OPTIONS, prune=True)
MAC = "02:0a:0b:0c:0d:04"


def parsed(page: str, html: str) -> ParsedPage:
    return parse_page(page, html, include_secrets=True)


def live_pages(**overrides: str) -> dict[str, ParsedPage]:
    html = {
        "services": RESTORE_SERVICES_HTML,
        "apphosting": RESTORE_APPHOSTING_HTML,
        "ipalloc": IPALLOC_HTML,
        "dosprotect": RESTORE_DOSPROTECT_HTML,
        "packetfilter": PACKETFILTER_HTML,
        "etherlan": ETHERLAN_HTML,
        **overrides,
    }
    return {page: parsed(page, body) for page, body in html.items()}


def live(pages: Mapping[str, ParsedPage]) -> Snapshot:
    return extract_snapshot(pages, ts="t", router_host="r")


def dump() -> Snapshot:
    return Snapshot(
        meta=META,
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


def dump_no_service_adds() -> Snapshot:
    # Remove planning is only unblocked on a page with no unblocked add step, so prune-focused
    # tests start from a dump whose services are all already on the router.
    base = dump()
    return replace(base, services=[s for s in base.services if s.name != "Mosh"])


def plan(snap: Snapshot, pages: Mapping[str, ParsedPage], options: RestoreOptions = OPTIONS) -> list[RestoreStep]:
    return build_restore_plan(diff_snapshots(snap, live(pages), pages=options.pages), snap, pages, options)


def kinds(steps: list[RestoreStep]) -> list[str]:
    return [f"{s.page}:{s.kind}" for s in steps]


def only(steps: list[RestoreStep], kind: str, contains: str | None = None) -> RestoreStep:
    matches = [s for s in steps if s.kind == kind and (contains is None or contains in s.description)]
    assert len(matches) == 1, [s.description for s in matches]
    return matches[0]


# --- fake router for execute_restore ------------------------------------------------------------------


@dataclass(frozen=True)
class Post:
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Get:
    status_code: int
    body: str = ""


class FakeRouter:
    """Records POSTs; answers GETs from a page -> HTML map (or a callable) and POSTs via `answer`."""

    def __init__(
        self,
        pages: Mapping[str, str] | Callable[[str], Get] | None = None,
        answer: Callable[[str, dict[str, str]], Post] | None = None,
    ) -> None:
        self._pages = pages
        self._answer = answer
        self.posted: list[tuple[str, dict[str, str]]] = []
        self.gets: list[str] = []

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> Post:
        body = dict(fields)
        self.posted.append((page, body))
        if self._answer is not None:
            return self._answer(page, body)
        return Post(302, {"location": f"/cgi-bin/{page}.ha"})

    def get_cgi_page(self, page: str) -> Get:
        self.gets.append(page)
        if callable(self._pages):
            return self._pages(page)
        if self._pages is None:
            raise AssertionError("get_cgi_page should not be called")
        return Get(200, self._pages[page])


def statuses(execution: Any) -> list[str]:
    return [s.status for s in execution.steps]


# --- planning -------------------------------------------------------------------------------------------


def test_restore_order_and_save_buttons_are_fixed():
    assert list(RESTORE_PAGE_ORDER) == [
        "services",
        "apphosting",
        "ipalloc",
        "packetfilter",
        "dosprotect",
        "wconfig",
        "wmacauth",
        "ippass",
        "etherlan",
        "dhcpserver",
    ]
    assert dict(FORM_SAVE_BUTTONS) == {
        "dosprotect": "Save",
        "wconfig": "Save",
        "etherlan": "Save",
        "dhcpserver": "Save",
        "ippass": "Save",
        "wmacauth": "Save",
    }


def test_build_restore_plan_adds_only_missing_services_and_forwards_in_order_with_add_payloads():
    steps = plan(dump(), live_pages())
    assert kinds(steps) == [
        "services:add-service",
        "apphosting:add-forward",
        "apphosting:add-forward",
        "packetfilter:skip",
        "dosprotect:form",
    ]
    add = steps[0]
    assert add.button == "Add"
    assert add.raw_payload is not None
    assert (
        add.raw_payload.items()
        >= {
            "Service": "Mosh",
            "extMinPort": "60001",
            "extMaxPort": "60010",
            "intStartPort": "60001",
            "protocol": "UDP",
            "Add": "Add",
        }.items()
    )
    assert [s.order for s in steps] == [1, 2, 3, 4, 5]


def test_forward_whose_service_is_not_in_the_dropdown_is_blocked_not_silently_dropped():
    steps = plan(dump(), live_pages())
    nope = only(steps, "add-forward", "Nope")
    assert nope.blocked is not None and "not offered" in nope.blocked
    mosh = only(steps, "add-forward", "Mosh")
    assert mosh.blocked is None
    assert mosh.raw_payload is not None
    assert mosh.raw_payload.items() >= {"service": "Mosh", "device": "aa:bb:cc:dd:ee:02"}.items()


def test_form_step_posts_only_differing_fields_with_the_page_save_button():
    form = only(plan(dump(), live_pages()), "form")
    assert form.page == "dosprotect"
    assert form.button == "Save"
    assert form.assignments == ["flood_protect=on"]
    assert form.raw_payload is not None
    assert form.raw_payload.items() >= {"flood_protect": "on", "Save": "Save"}.items()


def test_prune_emits_remove_steps_for_extra_rows_and_never_without_prune():
    snap = dump_no_service_adds()
    assert not any(s.kind.startswith("remove") for s in plan(snap, live_pages()))
    remove = only(plan(snap, live_pages(), PRUNE), "remove-service")
    assert remove.button == "Remove_2"
    assert "Stale" in remove.description
    assert remove.raw_payload is not None and "Remove_2" in remove.raw_payload


def test_prune_emits_only_one_real_remove_per_page_and_blocks_the_rest():
    pages = live_pages(services=RESTORE_SERVICES_HTML.replace(STALE_ROW, STALE_ROW + STALE2_ROW))
    removes = [s for s in plan(dump_no_service_adds(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    assert removes[0].blocked is None and removes[0].button == "Remove_2"
    assert removes[1].blocked is not None and "separate --prune runs" in removes[1].blocked


def test_an_earlier_unresolvable_extra_does_not_block_a_later_genuinely_removable_extra():
    rows = STALE_ROW + STALE_3_3_REMOVE3_ROW + STALE2_REMOVE4_ROW
    ambiguous_then_clean = RESTORE_SERVICES_HTML.replace(STALE_ROW, rows)
    pages = live_pages(services=ambiguous_then_clean)
    removes = [s for s in plan(dump_no_service_adds(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 3
    assert removes[0].blocked is not None and "ambiguous" in removes[0].blocked
    assert removes[1].blocked is not None and "ambiguous" in removes[1].blocked
    assert removes[2].blocked is None
    assert removes[2].button == "Remove_4"
    assert "Stale2" in removes[2].description


def test_missing_live_page_blocks_its_steps_instead_of_throwing():
    pages = live_pages()
    del pages["dosprotect"]
    form = only(plan(dump(), pages), "form")
    assert form.blocked is not None and "not fetched" in form.blocked


def test_form_step_never_assigns_a_disabled_control_even_if_the_dump_differs():
    steps = plan(dump(), live_pages(dosprotect=DISABLED_DOSPROTECT_HTML))
    assert not [s for s in steps if s.kind == "form" and s.page == "dosprotect"]


def test_a_missing_or_renamed_save_button_blocks_only_that_form_step():
    steps = plan(dump(), live_pages(dosprotect=DOSPROTECT_RENAMED_BUTTON_HTML))
    assert kinds(steps) == [
        "services:add-service",
        "apphosting:add-forward",
        "apphosting:add-forward",
        "packetfilter:skip",
        "dosprotect:form",
    ]
    form = only(steps, "form")
    assert form.blocked is not None and "not found" in form.blocked
    assert steps[0].blocked is None
    assert steps[3].blocked is None


def test_an_unrelated_table_on_the_same_page_does_not_shift_remove_button_indices():
    html = RESTORE_SERVICES_HTML.replace(
        "<table><tr><th>Service Name</th>", f"{PORT_STATUS_TABLE}<table><tr><th>Service Name</th>"
    )
    pages = live_pages(services=html)
    # The parser flattens both >=3-column tables into one list; the snapshot tolerates the foreign rows.
    assert len(pages["services"].tables) == 4
    remove = only(plan(dump_no_service_adds(), pages, PRUNE), "remove-service")
    assert remove.blocked is None
    assert remove.button == "Remove_2"
    assert "Stale" in remove.description


def test_a_row_button_count_mismatch_blocks_the_remove_instead_of_guessing():
    html = RESTORE_SERVICES_HTML.replace(
        STALE_ROW + "</table>",
        STALE_ROW + "<tr><td>OrphanSvc</td><td>9-9</td><td>9</td><td>TCP</td><td></td></tr></table>",
    )
    removes = [s for s in plan(dump(), live_pages(services=html), PRUNE) if s.kind == "remove-service"]
    assert removes
    for step in removes:
        assert step.blocked is not None and "cannot map" in step.blocked


def test_remove_matches_services_by_the_name_column_only_not_any_cell():
    remove = only(plan(dump_no_service_adds(), live_pages(services=TCP_NAMED_SERVICE_HTML), PRUNE), "remove-service")
    assert remove.blocked is None
    assert remove.button == "Remove_2"
    assert "TCP" in remove.description


def test_duplicate_rows_sharing_a_name_block_the_remove_instead_of_guessing():
    removes = [s for s in plan(dump(), live_pages(services=DUP_NAME_HTML), PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    for step in removes:
        assert step.blocked is not None and "ambiguous" in step.blocked


def test_form_step_description_reports_only_the_fields_actually_being_applied():
    pages = live_pages(dosprotect=TWO_FIELD_DOSPROTECT_HTML)
    custom = replace(dump(), forms={"dosprotect": {"flood_protect": "on", "log_attacks": "yes"}})
    form = only(plan(custom, pages), "form")
    assert form.assignments == ["log_attacks=yes"]
    assert "flood_protect" not in form.description
    assert "log_attacks" in form.description


def test_c1_a_services_page_with_a_pending_add_blocks_its_removes_in_the_same_run():
    steps = plan(dump(), live_pages(), PRUNE)
    assert only(steps, "add-service").blocked is None
    remove = only(steps, "remove-service")
    assert remove.blocked is not None and "pending adds" in remove.blocked
    assert remove.raw_payload is None


def test_c1_with_no_add_pending_on_the_page_the_remove_stays_unblocked():
    steps = plan(dump_no_service_adds(), live_pages(), PRUNE)
    assert not [s for s in steps if s.kind == "add-service"]
    remove = only(steps, "remove-service")
    assert remove.blocked is None
    assert remove.button == "Remove_2"


def test_c1_an_apphosting_page_with_a_pending_add_blocks_its_forward_removes_too():
    steps = plan(dump(), live_pages(apphosting=APPHOSTING_EXTRA_HTML), PRUNE)
    assert only(steps, "add-forward", "Mosh").blocked is None
    remove = only(steps, "remove-forward")
    assert remove.blocked is not None and "pending adds" in remove.blocked


def test_c1_an_apphosting_page_with_no_pending_add_still_removes_the_extra_forward():
    snap = replace(dump(), forwards=[SnapshotForward("custom_ssh", "host-b", "aa:bb:cc:dd:ee:01")])
    steps = plan(snap, live_pages(apphosting=APPHOSTING_EXTRA_HTML), PRUNE)
    assert not [s for s in steps if s.kind == "add-forward"]
    remove = only(steps, "remove-forward")
    assert remove.blocked is None
    assert remove.button == "Remove_2"


def test_c1_a_blocked_for_pending_adds_remove_does_not_consume_the_one_remove_per_page_slot():
    pages = live_pages(services=RESTORE_SERVICES_HTML.replace(STALE_ROW, STALE_ROW + STALE2_ROW))
    removes = [s for s in plan(dump(), pages, PRUNE) if s.kind == "remove-service"]
    assert len(removes) == 2
    for step in removes:
        assert step.blocked is not None and "pending adds" in step.blocked


def test_i7_the_etherlan_form_is_not_planned_when_pages_leaves_it_out():
    snap = replace(dump(), forms={**dump().forms, "etherlan": {"lan_mtu": "1400"}})
    steps = plan(snap, live_pages(), replace(OPTIONS, pages=("services", "apphosting", "dosprotect")))
    assert not [s for s in steps if s.page == "etherlan"]
    assert not [s for s in plan(dump(), live_pages()) if s.page == "etherlan"]  # not captured -> not planned


def test_i7_an_etherlan_form_captured_in_the_dump_plans_exactly_one_form_save():
    snap = replace(dump(), forms={**dump().forms, "etherlan": {"lan_mtu": "1400"}})
    lan_steps = [s for s in plan(snap, live_pages()) if s.page == "etherlan"]
    assert len(lan_steps) == 1
    step = lan_steps[0]
    assert step.kind == "form" and step.button == "Save"
    assert step.assignments == ["lan_mtu=1400"]
    assert step.raw_payload is not None
    assert step.raw_payload.items() >= {"lan_mtu": "1400", "Save": "Save"}.items()


def test_add_service_resolves_the_protocol_against_the_live_select_option_values():
    import re

    html = re.sub(r'<select name="protocol">[\s\S]*?</select>', REAL_PROTOCOL_SELECT, RESTORE_SERVICES_HTML)
    pages = live_pages(services=html)
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
    wanted = replace(
        dump_no_service_adds(),
        forwards=live(pages).forwards,
        reservations=[
            SnapshotReservation(MAC, "192.168.1.67"),
            SnapshotReservation("02:0a:0b:0c:0d:02", "192.168.1.64"),
            SnapshotReservation("02:0a:0b:0c:0d:03", "192.168.1.71"),
        ],
    )
    steps = plan(wanted, pages)
    names = kinds(steps)
    assert names.index("ipalloc:reserve") < names.index("packetfilter:skip")
    reserves = [s for s in steps if s.kind == "reserve"]
    assert [s.description for s in reserves] == [
        f"reserve 192.168.1.67 for {MAC} (currently DHCP)",
        "reserve 192.168.1.71 for 02:0a:0b:0c:0d:03 (currently 192.168.1.70)",
    ]
    assert reserves[0].button == f"Allocate_{MAC}"
    assert reserves[0].raw_payload == {f"Allocate_{MAC}": "Allocate"}
    assert reserves[0].follow_up == RestoreFollowUp("ipalloc", f"alloc_{MAC}", "192.168.1.67", "Save")
    assert all(s.blocked is None for s in reserves)


def test_a_reservation_for_a_device_not_on_the_ip_allocation_page_is_blocked():
    pages = live_pages()
    wanted = replace(
        dump_no_service_adds(),
        forwards=live(pages).forwards,
        reservations=[SnapshotReservation("00:11:22:33:44:55", "192.168.1.80")],
    )
    reserve = only(plan(wanted, pages), "reserve")
    assert reserve.blocked is not None and "not present on IP Allocation page" in reserve.blocked
    assert reserve.raw_payload is None


def test_extra_reservations_are_never_released_even_with_prune():
    pages = live_pages()
    wanted = replace(dump_no_service_adds(), forwards=live(pages).forwards, reservations=[])
    assert not [s for s in plan(wanted, pages, PRUNE) if s.page == "ipalloc"]


def test_wconfig_form_step_uses_the_save_button_with_its_rendered_value():
    pages = live_pages(wconfig=WCONFIG_SAVE_DOTS_HTML)
    snapshot = live(pages)
    wanted = replace(
        dump_no_service_adds(), forwards=snapshot.forwards, forms={**snapshot.forms, "wconfig": {"maxclients": "81"}}
    )
    form = only([s for s in plan(wanted, pages) if s.page == "wconfig"], "form")
    assert form.button == "Save"
    assert form.raw_payload is not None
    assert form.raw_payload.items() >= {"maxclients": "81", "Save": "Save..."}.items()


def test_a_reserve_step_posts_only_the_allocate_button_never_a_sticky_entry_block_alloc_select():
    pages = live_pages(ipalloc=IPALLOC_STICKY_HTML)
    assert any(s.name == "alloc_aa:bb:cc:dd:ee:ff" for s in pages["ipalloc"].selects)
    wanted = replace(
        dump_no_service_adds(), forwards=live(pages).forwards, reservations=[SnapshotReservation(MAC, "192.168.1.67")]
    )
    reserve = only(plan(wanted, pages), "reserve")
    assert reserve.blocked is None
    assert reserve.raw_payload == {f"Allocate_{MAC}": "Allocate"}
    assert reserve.display_payload == {f"Allocate_{MAC}": "Allocate"}


def test_add_forward_matches_a_custom_service_the_router_lists_with_a_star_prefix():
    starred = RESTORE_APPHOSTING_HTML.replace(
        '<option value="Mosh">Mosh</option>',
        '<option value="*Mosh">*Mosh</option><option value="SSH server">SSH server</option>',
    )
    pages = live_pages(apphosting=starred)
    wanted = replace(
        dump_no_service_adds(), forwards=[*live(pages).forwards, SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")]
    )
    add = only(plan(wanted, pages), "add-forward")
    assert add.blocked is None
    assert add.raw_payload is not None
    assert add.raw_payload.items() >= {"service": "*Mosh", "device": "aa:bb:cc:dd:ee:02"}.items()


def test_prune_removes_a_forward_before_the_custom_service_it_references():
    services = RESTORE_SERVICES_HTML.replace(STALE_ROW, ZZ_TEST_SERVICE_ROW)
    apphosting = RESTORE_APPHOSTING_HTML.replace(
        '<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>',
        '<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
        '<tr><td>zz_test</td><td>host-a</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>',
    )
    pages = live_pages(services=services, apphosting=apphosting)
    snapshot = live(pages)
    wanted = replace(
        dump_no_service_adds(),
        services=[s for s in snapshot.services if s.name != "zz_test"],
        forwards=[f for f in snapshot.forwards if f.service != "zz_test"],
    )
    steps = plan(wanted, pages, PRUNE)
    remove_forward = only(steps, "remove-forward")
    remove_service = only(steps, "remove-service")
    assert remove_forward.blocked is None and remove_service.blocked is None
    assert remove_forward.order < remove_service.order


def test_forward_for_a_service_added_earlier_in_the_same_plan_is_deferred_not_blocked():
    steps = plan(dump(), live_pages(apphosting=APPHOSTING_WITHOUT_MOSH_HTML))
    assert only(steps, "add-service").blocked is None
    mosh = only(steps, "add-forward", "Mosh")
    assert mosh.blocked is None
    assert mosh.raw_payload is None
    assert mosh.deferred is not None
    assert mosh.deferred.forward == SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")
    assert "added earlier in this run" in mosh.deferred.reason
    nope = only(steps, "add-forward", "Nope")
    assert nope.blocked is not None and "not offered" in nope.blocked
    assert nope.deferred is None


def test_a_deferred_forward_counts_as_a_pending_add_so_apphosting_removes_stay_blocked_under_prune():
    wanted = replace(dump(), forwards=[f for f in dump().forwards if f.service != "custom_ssh"])
    remove = only(plan(wanted, live_pages(apphosting=APPHOSTING_WITHOUT_MOSH_HTML), PRUNE), "remove-forward")
    assert remove.blocked is not None and "pending adds" in remove.blocked


# --- execution: the executor parses router HTML through the real parser -------------------------------


def reserve_step(mac: str, ip: str) -> RestoreStep:
    return RestoreStep(
        order=1,
        kind="reserve",
        page="ipalloc",
        description=f"reserve {ip} for {mac} (currently DHCP)",
        button=f"Allocate_{mac}",
        raw_payload={f"Allocate_{mac}": "Allocate"},
        follow_up=RestoreFollowUp("ipalloc", f"alloc_{mac}", ip, "Save"),
    )


SKIP_STEP = RestoreStep(order=2, kind="skip", page="packetfilter", description="skip")


def test_execute_restore_performs_allocate_reads_the_entry_form_then_posts_save_with_the_chosen_address():
    router = FakeRouter({"ipalloc": entry_page_html(MAC, ["192.168.1.67", "192.168.1.68"])})
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert router.posted == [
        ("ipalloc", {f"Allocate_{MAC}": "Allocate"}),
        ("ipalloc", {f"alloc_{MAC}": "192.168.1.67", "Save": "Save"}),
    ]
    assert statuses(execution) == ["applied"]
    assert execution.stopped_at is None


def test_execute_restore_fails_the_reserve_step_without_posting_save_when_the_address_is_not_offered():
    router = FakeRouter({"ipalloc": entry_page_html(MAC, ["192.168.1.68"])}, answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67"), SKIP_STEP])
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]
    assert statuses(execution) == ["failed", "not-run"]
    assert execution.steps[0].error is not None and "not offered" in execution.steps[0].error
    assert execution.stopped_at == 1


def test_execute_restore_fails_the_reserve_step_when_the_entry_form_belongs_to_a_different_device():
    router = FakeRouter(
        {"ipalloc": entry_page_html("aa:bb:cc:dd:ee:ff", ["192.168.1.67"])}, answer=lambda p, f: Post(302)
    )
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and f"did not open for {MAC}" in execution.steps[0].error


def test_execute_restore_fails_the_reserve_step_when_the_follow_up_save_post_returns_a_non_2xx_3xx_status():
    calls: list[int] = []

    def answer(page: str, fields: dict[str, str]) -> Post:
        calls.append(1)
        return Post(302) if len(calls) == 1 else Post(500)

    router = FakeRouter({"ipalloc": entry_page_html(MAC, ["192.168.1.67", "192.168.1.68"])}, answer=answer)
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and "HTTP 500" in execution.steps[0].error
    assert execution.stopped_at == 1


def test_execute_restore_fails_the_reserve_step_when_get_cgi_page_throws_without_posting_save():
    def boom(page: str) -> Get:
        raise RuntimeError("socket hang up")

    router = FakeRouter(boom, answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and "socket hang up" in execution.steps[0].error
    assert execution.stopped_at == 1
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]


def test_execute_restore_fails_the_reserve_step_when_the_follow_up_get_returns_a_non_200_status():
    router = FakeRouter(lambda page: Get(302, ""), answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and "unexpected HTTP 302 reading ipalloc" in execution.steps[0].error
    assert execution.stopped_at == 1


def test_execute_restore_refuses_to_post_an_allocation_value_that_is_not_a_dotted_ipv4_address():
    router = FakeRouter({"ipalloc": entry_page_html(MAC, ["192.168.1.67"])}, answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "normal")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None
    assert "refusing to post non-IPv4 allocation value 'normal'" in execution.steps[0].error
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]
    assert execution.stopped_at == 1


def test_execute_restore_posts_the_save_button_rendered_value_not_its_name():
    router = FakeRouter(
        {"ipalloc": entry_page_html_with_save(MAC, ["192.168.1.67"], "Save...")}, answer=lambda p, f: Post(302)
    )
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["applied"]
    assert router.posted[1] == ("ipalloc", {f"alloc_{MAC}": "192.168.1.67", "Save": "Save..."})


def test_execute_restore_fails_the_reserve_step_when_the_entry_form_has_no_save_button():
    router = FakeRouter(
        {"ipalloc": entry_page_html_with_save(MAC, ["192.168.1.67"], None)}, answer=lambda p, f: Post(302)
    )
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert (
        execution.steps[0].error is not None and "IP Allocation Entry has no 'Save' button" in execution.steps[0].error
    )
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]
    assert execution.stopped_at == 1


def test_execute_restore_fails_the_reserve_step_when_the_matching_address_option_is_disabled():
    router = FakeRouter({"ipalloc": entry_page_html_disabled_option(MAC)}, answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and "not offered" in execution.steps[0].error
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]
    assert execution.stopped_at == 1


def test_a_form_save_that_redirects_to_the_wifi_warning_page_is_confirmed_with_continue():
    def answer(page: str, fields: dict[str, str]) -> Post:
        if "Continue" in fields:
            return Post(302, {"location": "/cgi-bin/wconfig.ha"})
        return Post(302, {"location": "/cgi-bin/wifiwarn_advanced.ha"})

    def pages(page: str) -> Get:
        return Get(200, WIFI_WARN_HTML if page == "wifiwarn_advanced" else "<html></html>")

    router = FakeRouter(pages, answer=answer)
    step = RestoreStep(
        order=1,
        kind="form",
        page="wconfig",
        description="save wconfig",
        button="Save",
        raw_payload={"maxclients": "81", "Save": "Save..."},
    )
    execution = execute_restore(router, [step])
    # Continue is posted to the owning form's action (wconfig.ha), not to the warning page.
    assert router.posted == [
        ("wconfig", {"maxclients": "81", "Save": "Save..."}),
        ("wconfig", {"Continue": "Continue"}),
    ]
    assert router.gets == ["wifiwarn_advanced"]
    assert statuses(execution) == ["applied"]
    assert execution.steps[0].location == "/cgi-bin/wconfig.ha"


def test_a_wifi_warning_page_without_a_continue_button_fails_the_step_without_a_second_post():
    router = FakeRouter(
        lambda page: Get(200, WIFI_WARN_NO_CONTINUE_HTML),
        answer=lambda p, f: Post(302, {"location": "/cgi-bin/wifiwarn_advanced.ha"}),
    )
    step = RestoreStep(order=1, kind="form", page="wconfig", description="save", button="Save", raw_payload={"x": "1"})
    execution = execute_restore(router, [step])
    assert [p for p, _ in router.posted] == ["wconfig"]
    assert statuses(execution) == ["failed"]
    assert execution.steps[0].error is not None and "Continue" in execution.steps[0].error
    assert execution.stopped_at == 1


def deferred_steps() -> list[RestoreStep]:
    return [
        RestoreStep(
            order=1,
            kind="add-service",
            page="services",
            description="add service Mosh",
            button="Add",
            raw_payload={"Service": "Mosh", "Add": "Add"},
        ),
        RestoreStep(
            order=2,
            kind="add-forward",
            page="apphosting",
            description="add forward Mosh -> host-a",
            deferred=RestoreDeferredForward(
                SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02"), "service 'Mosh' is added earlier in this run"
            ),
        ),
        RestoreStep(
            order=3,
            kind="form",
            page="dosprotect",
            description="save",
            button="Save",
            raw_payload={"flood_protect": "on", "Save": "Save"},
        ),
    ]


def test_execute_restore_re_reads_the_nat_gaming_dropdown_and_posts_a_deferred_forward_once_its_service_exists():
    router = FakeRouter({"apphosting": APPHOSTING_WITH_STAR_MOSH_HTML})
    execution = execute_restore(router, deferred_steps())
    assert router.gets == ["apphosting"]
    assert [p for p, _ in router.posted] == ["services", "apphosting", "dosprotect"]
    assert router.posted[1][1].items() >= {"service": "*Mosh", "device": "aa:bb:cc:dd:ee:02", "Add": "Add"}.items()
    assert statuses(execution) == ["applied", "applied", "applied"]
    assert execution.stopped_at is None


def test_execute_restore_leaves_a_deferred_forward_blocked_when_the_dropdown_still_lacks_its_service():
    router = FakeRouter({"apphosting": APPHOSTING_WITHOUT_MOSH_HTML}, answer=lambda p, f: Post(302))
    execution = execute_restore(router, deferred_steps())
    assert [p for p, _ in router.posted] == ["services", "dosprotect"]
    assert statuses(execution) == ["applied", "blocked", "applied"]
    assert execution.steps[1].error is not None and "not offered" in execution.steps[1].error
    assert execution.stopped_at is None


def test_execute_restore_records_a_failed_deferred_forward_when_re_reading_apphosting_throws():
    def boom(page: str) -> Get:
        raise RuntimeError("Timed out reading apphosting.ha")

    router = FakeRouter(boom, answer=lambda p, f: Post(302))
    execution = execute_restore(router, deferred_steps())
    assert [p for p, _ in router.posted] == ["services"]
    assert statuses(execution) == ["applied", "failed", "not-run"]
    assert execution.steps[1].error is not None and "Timed out" in execution.steps[1].error
    assert execution.stopped_at == 2


# --- unchecked controls -------------------------------------------------------------------------------


def test_a_box_unchecked_in_the_dump_but_checked_live_is_restored_by_omitting_it_from_the_save_post():
    pages = live_pages(dosprotect=DOSPROTECT_CHECKBOX_HTML)
    snapshot = live(pages)
    assert snapshot.forms["dosprotect"] == {"reflexive": "on", "algsip": UNCHECKED}
    wanted = replace(
        dump_no_service_adds(),
        forwards=snapshot.forwards,
        forms={"dosprotect": {"reflexive": UNCHECKED, "algsip": UNCHECKED}},
    )
    form = only([s for s in plan(wanted, pages) if s.page == "dosprotect"], "form")
    assert form.blocked is None
    assert form.raw_payload == {"Save": "Save"}
    assert form.display_payload is not None and "reflexive" not in form.display_payload
    assert "reflexive: on -> <unchecked>" in form.description


def test_a_box_checked_in_the_dump_but_unchecked_live_is_restored_by_posting_its_value():
    pages = live_pages(dosprotect=DOSPROTECT_CHECKBOX_HTML)
    wanted = replace(
        dump_no_service_adds(), forwards=live(pages).forwards, forms={"dosprotect": {"reflexive": "on", "algsip": "on"}}
    )
    form = only([s for s in plan(wanted, pages) if s.page == "dosprotect"], "form")
    assert form.raw_payload == {"reflexive": "on", "algsip": "on", "Save": "Save"}


def test_identical_unchecked_state_on_both_sides_is_not_a_difference():
    pages = live_pages(dosprotect=DOSPROTECT_CHECKBOX_HTML)
    snapshot = live(pages)
    wanted = replace(
        dump_no_service_adds(),
        forwards=snapshot.forwards,
        forms={"dosprotect": {"reflexive": "on", "algsip": UNCHECKED}},
    )
    assert diff_snapshots(wanted, snapshot).forms == {}


def test_a_text_field_whose_dump_value_is_literally_unchecked_is_assigned_not_omitted():
    pages = live_pages(dosprotect=TEXT_FIELD_LITERAL_UNCHECKED_HTML)
    wanted = replace(
        dump_no_service_adds(),
        forwards=live(pages).forwards,
        forms={"dosprotect": {"display_label": UNCHECKED, "reflexive": UNCHECKED}},
    )
    form = only([s for s in plan(wanted, pages) if s.page == "dosprotect"], "form")
    # The sentinel only means "off" for a checkable control; for the text input it is the literal value.
    assert form.raw_payload == {"display_label": UNCHECKED, "Save": "Save"}


# --- end to end: dump pages -> plan -> execute -> convergence ----------------------------------------


def test_end_to_end_plan_from_real_pages_executes_and_converges_against_the_post_restore_pages():
    from integration_html import APPHOSTING_HTML, SERVICES_HTML

    # The dump captured the router when Mosh existed (snapshot.test.ts pages); the live router
    # (restore.test.ts pages) lost Mosh and its forward and gained a Stale service.
    dump_pages = {"services": parsed("services", SERVICES_HTML), "apphosting": parsed("apphosting", APPHOSTING_HTML)}
    wanted = extract_snapshot(dump_pages, ts="t", router_host="r")
    pages = live_pages()
    steps = plan(wanted, pages)
    assert kinds(steps) == ["services:add-service", "apphosting:add-forward", "packetfilter:skip"]
    # The forward is deferred because its service is added in this run and the dropdown lacks it... unless it
    # already offers "Mosh" (as this page does), in which case it is planned directly.
    forward = only(steps, "add-forward")
    assert forward.deferred is None and forward.raw_payload is not None

    router = FakeRouter()
    execution = execute_restore(router, steps)
    assert statuses(execution) == ["applied", "applied", "skipped"]
    assert [p for p, _ in router.posted] == ["services", "apphosting"]
    assert router.posted[0][1].items() >= {"Service": "Mosh", "protocol": "UDP", "Add": "Add"}.items()
    assert router.posted[1][1].items() >= {"service": "Mosh", "device": "aa:bb:cc:dd:ee:02", "Add": "Add"}.items()

    # After the router applied the adds it renders the snapshot.test.ts pages again: converged without
    # --prune (Stale stays), not converged with --prune.
    after = extract_snapshot(dump_pages, ts="t2", router_host="r")
    assert restore_converged(diff_snapshots(wanted, after), prune=False) is True
    still_stale = extract_snapshot(pages, ts="t2", router_host="r")
    assert restore_converged(diff_snapshots(wanted, still_stale), prune=True) is False
