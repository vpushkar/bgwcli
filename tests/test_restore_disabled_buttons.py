"""restore never posts a button the live page renders disabled."""

from dataclasses import replace

from integration_html import entry_page_html
from page_builders import ipalloc_page
from test_integration_restore import FakeRouter, Post, reserve_step, statuses

from bgwcli.restore import _reserve_step, execute_restore
from bgwcli.snapshot import SnapshotReservation

MAC = "02:0a:0b:0c:0d:02"


def test_a_disabled_allocate_button_blocks_the_reserve_step():
    live = ipalloc_page(extra_buttons=[])
    live = replace(live, buttons=[replace(b, disabled=True) for b in live.buttons])
    step = _reserve_step(SnapshotReservation(MAC, "192.168.1.64"), None, {"ipalloc": live})
    assert step.blocked is not None and "disabled" in step.blocked
    assert not step.raw_payload


def test_a_disabled_save_on_the_entry_form_blocks_the_step_without_posting_it():
    body = entry_page_html(MAC, ["192.168.1.67"]).replace('name="Save"', 'name="Save" disabled')
    router = FakeRouter({"ipalloc": body}, answer=lambda p, f: Post(302))
    execution = execute_restore(router, [reserve_step(MAC, "192.168.1.67")])
    assert statuses(execution) == ["blocked"]
    assert "disabled" in (execution.steps[0].error or "")
    assert [",".join(f) for _, f in router.posted] == [f"Allocate_{MAC}"]
    assert execution.stopped_at is None
