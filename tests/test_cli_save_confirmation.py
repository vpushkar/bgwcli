"""Configuration writes require the gateway acknowledgement, not just a redirect."""

from __future__ import annotations

import re

import pytest
from test_allocation_confirmation import IP_A, MAC_A, MAC_B, allocation_html

from bgwcli import restore
from bgwcli.cli import _post_and_confirm
from bgwcli.exit_codes import NEGATIVE_ANSWER, NO_ANSWER
from bgwcli.save_result import decision_for_step
from bgwcli.types import HttpResponse

SAVED = '<div id="error-message-text" style="color: red">Changes <strong>saved</strong>.</div>'


class Client:
    def __init__(self, page, bodies):
        self.page = page
        self.bodies = bodies
        self.posts = []
        self.reads = 0

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        return HttpResponse(302, "Found", {"location": f"/cgi-bin/{page}.ha"}, "", "https://synthetic.invalid/")

    def get_cgi_page(self, page):
        assert page == self.page
        assert self.reads < 10, "polling must terminate"
        body = self.bodies[min(self.reads, len(self.bodies) - 1)]
        self.reads += 1
        return HttpResponse(200, "OK", {}, body, "https://synthetic.invalid/")


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0.0}

    def sleep(seconds):
        assert seconds > 0
        state["now"] += seconds

    monkeypatch.setattr(restore, "monotonic", lambda: state["now"])
    monkeypatch.setattr(restore, "sleep", sleep)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_TIMEOUT_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_POLL_SECONDS", 1.0, raising=False)
    return state


@pytest.mark.parametrize(
    ("page", "fields"),
    [
        ("dosprotect", {"Save": "Save"}),
        ("apphosting", {"Add": "Add"}),
        ("apphosting", {"Remove_1": "Remove"}),
        ("services", {"Add": "Add"}),
        ("services", {"Remove_1": "Remove"}),
    ],
)
def test_configuration_save_and_nat_changes_wait_for_acknowledgement(clock, page, fields):
    client = Client(page, ["<html>Saving</html>", SAVED])

    status, location = _post_and_confirm(client, page, fields)

    assert status == 302 and location == f"/cgi-bin/{page}.ha"
    assert client.reads == 2
    assert client.posts == [(page, fields)]
    assert clock["now"] >= 1


def test_configuration_save_timeout_is_an_error_without_reposting(clock):
    client = Client("dosprotect", ["<html>Saving</html>"])

    step = _post_and_confirm(client, "dosprotect", {"Save": "Save"})

    assert client.posts == [("dosprotect", {"Save": "Save"})]
    assert clock["now"] >= 2
    # An unconfirmed write is "could not answer" (exit 2), the same code the Wi-Fi and LAN save
    # paths report for an unconfirmed save; it is returned for a structured report, not raised.
    assert step.status == "failed" and "Timed out" in step.error
    assert decision_for_step(step, requested=True).exit_code == NO_ANSWER


def test_configuration_save_rejected_by_an_error_banner_is_a_negative_answer(clock):
    rejected = (
        '<img id="error-message-icon" src="/images/icon_error.png" alt="alert" />'
        '<div id="error-message-text"> A required setting is empty <br /></div>'
    )
    client = Client("dosprotect", [rejected])

    step = _post_and_confirm(client, "dosprotect", {"Save": "Save"})

    # An explicit rejection is a definitive answer (exit 1), unlike a missing acknowledgement.
    assert step.status == "failed"
    assert re.match("router rejected the change: A required setting is empty", step.error)
    assert decision_for_step(step, requested=True).exit_code == NEGATIVE_ANSWER
    assert client.posts == [("dosprotect", {"Save": "Save"})]


def test_post_and_confirm_reads_a_no_changes_answer_as_unchanged(clock):
    """`_post_and_confirm` is the action path: an action requests no field values, so the gateway's
    "No changes detected. Save not performed." is a complete answer: unchanged (exit 0), nothing
    saved. The `set`/`submit` paths verify requested values against the live form first and answer
    0 or 1; see tests/test_save_contract.py, which drives them through `cli.main`."""
    no_changes = '<div id="error-message-text">No changes detected. Save not performed.</div>'
    client = Client("wconfig", [no_changes])

    step = _post_and_confirm(client, "wconfig", {"Save": "Save"})

    assert step.status == "unchanged" and step.error is None and step.write_performed is False
    assert decision_for_step(step, requested=False).exit_code == 0
    assert client.posts == [("wconfig", {"Save": "Save"})]


def test_diagnostic_submit_does_not_require_a_configuration_saved_message(clock):
    client = Client("diag", ["<textarea>ping running</textarea>"])

    status, _ = _post_and_confirm(client, "diag", {"Ping": "Ping"})

    assert status == 302
    assert client.reads == 1  # Existing error-banner check only, not a success-message poll.
    assert clock["now"] == 0


def test_action_on_a_saveless_page_still_reports_a_banner_after_the_redirect(clock):
    banner = (
        '<img id="error-message-icon" src="/images/icon_error.png">'
        '<div id="error-message-text">A required setting is empty</div>'
    )
    client = Client("diag", [banner])

    step = _post_and_confirm(client, "diag", {"Ping": "Ping"})

    assert step.status == "failed"
    assert step.error == "router rejected the change: A required setting is empty"
    assert step.write_attempted is True and step.write_response_received is True
    assert decision_for_step(step, requested=True).exit_code == NEGATIVE_ANSWER
    assert client.posts == [("diag", {"Ping": "Ping"})]


def test_direct_allocation_save_waits_for_acknowledgement_and_matching_mac_ip(clock):
    client = Client("ipalloc", [SAVED, allocation_html(MAC_A.upper(), IP_A)])
    fields = {f"alloc_{MAC_A}": IP_A, "Save": "Save"}

    _post_and_confirm(client, "ipalloc", fields)

    assert client.reads == 2
    assert client.posts == [("ipalloc", fields)]


def test_direct_allocation_save_rejects_another_macs_fixed_row(clock):
    client = Client("ipalloc", [allocation_html(MAC_B, IP_A, banner=SAVED)])
    fields = {f"alloc_{MAC_A}": IP_A, "Save": "Save"}

    step = _post_and_confirm(client, "ipalloc", fields)

    assert client.posts == [("ipalloc", fields)]
    assert step.status == "failed" and re.search("Timed out.*fixed allocation", step.error)
    assert decision_for_step(step, requested=True).exit_code == NO_ANSWER
