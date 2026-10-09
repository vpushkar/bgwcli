"""A verification poll cut by the parser's bounds after an acknowledgement is an unreadable poll, not a crash.

"Changes saved" was observed, so the write happened: a later poll body over the parser's bounds must not
escape the wait (losing the acknowledgement and the performed evidence). It is recorded like any
unreadable poll and the wait goes on; at the deadline the step is acknowledged-but-unverifiable (exit 2,
SnapshotExtractionError). A cut body before anything was acknowledged (the write's own answer or the first
poll) is an unreadable poll like a Please-wait body: the wait keeps polling and the deadline reports the
not-acknowledged shape with the write evidence.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from page_builders import checkbox, dosprotect_page
from save_helpers import SAVED_RED
from test_autorestore import Fetcher, make_dump, reset_pages
from test_restore_structural_read_faults import _UnreadableVerificationRouter
from test_unchecked_needs_readable_form import PLEASE_WAIT, _restore, box_form

from bgwcli import parser as parser_module
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import UNCHECKED, SnapshotService
from bgwcli.types import HttpResponse

# The element bound is lowered for this module so a small body is cut; the real 150000-element
# bound is exercised in test_parser_bounds.py and test_truncated_consumers.py.
TEST_MAX_ELEMENTS = 1000
CUT = "<html><body>" + "<p>x</p>" * (TEST_MAX_ELEMENTS + 100) + "</body></html>"


@pytest.fixture(autouse=True)
def small_element_bound(monkeypatch):
    monkeypatch.setattr(parser_module, "MAX_ELEMENTS", TEST_MAX_ELEMENTS)


def test_acknowledged_save_whose_every_poll_is_cut_is_unverifiable(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [CUT])
    assert code == 2
    assert step["status"] == "failed" and step["errorType"] == "SnapshotExtractionError"
    assert step["acknowledgementObserved"] is True and step["writePerformed"] is True
    assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
    assert "stateObserved" not in step
    assert "requested state could not be read" in step["error"]
    assert posts == 1


def test_a_cut_poll_followed_by_a_readable_verifying_poll_is_applied(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [CUT, box_form(None)])
    assert code == 0 and step["status"] == "applied" and step["stateObserved"] is True
    assert posts == 1


def test_a_cut_first_read_polls_to_the_deadline_exactly_like_a_please_wait_first_read(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    # The write's own answer is cut and so is every poll: nothing was acknowledged, so the wait behaves
    # like an all-Please-wait run and the step carries the write evidence.
    code, out, step, posts = _restore(tmp_env, capsys, monkeypatch, CUT, [CUT])
    wait_code, wait_out, wait_step, wait_posts = _restore(tmp_env, capsys, monkeypatch, PLEASE_WAIT, [PLEASE_WAIT])
    assert code == 2 and step["status"] == "failed"
    assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
    assert step["acknowledgementObserved"] is False
    assert out["writeUnanswered"] is True and wait_out["writeUnanswered"] is True
    assert "Changes saved was not observed" in step["error"]
    assert posts == 1
    assert (wait_code, wait_posts) == (code, posts)
    assert step == wait_step


def test_a_cut_first_read_followed_by_an_acknowledged_verifying_poll_is_applied(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, CUT, [box_form(None, SAVED_RED)])
    assert code == 0 and step["status"] == "applied" and step["stateObserved"] is True
    assert posts == 1


class _CutRouter(_UnreadableVerificationRouter):
    def get_cgi_page(self, page, *, auth=True):
        if page == "dosprotect":
            return HttpResponse(200, "OK", {}, CUT, "https://router.local/cgi-bin/dosprotect.ha")
        return super(_UnreadableVerificationRouter, self).get_cgi_page(page, auth=auth)


def test_autorestore_reports_verification_unavailable_for_a_cut_poll_without_a_closing_read(clock, tmp_path):
    dump = replace(
        make_dump(), services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")], forwards=[], reservations=[],
        forms={"dosprotect": {"flag": UNCHECKED}},
    )
    reset = reset_pages()
    reset["dosprotect"] = dosprotect_page(fields=[checkbox("flag", "on", checked=True)])
    readable = reset_pages()
    readable["dosprotect"] = dosprotect_page(fields=[checkbox("flag", "on", checked=False)])
    router = _CutRouter()
    store = RecoveryCheckpoint("router.local", dump, None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset, readable)
    result = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=lambda _: None, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2, (result.reason, result.passes)
    assert "verification-unavailable" in result.reason and "failure 1/3" in result.reason
    assert result.write_unanswered is False
    assert len(fetcher.calls) == 1  # no closing read after the acknowledged save
    step = [s for s in result.passes[0]["steps"] if s["status"] == "failed"][0]
    assert step["errorType"] == "SnapshotExtractionError" and step["acknowledgementObserved"] is True
    assert step["writePerformed"] is True


class _UnansweredDosprotectRouter(_UnreadableVerificationRouter):
    """The dosprotect save is answered by `body` and every re-read of the page is `body` too."""

    def __init__(self, body):
        super().__init__()
        self.body = body

    def post_cgi_page(self, page, fields):
        if page != "dosprotect":
            return super(_UnreadableVerificationRouter, self).post_cgi_page(page, fields)
        self.posts.append((page, dict(fields)))
        return HttpResponse(200, "OK", {}, self.body, f"https://router.local/cgi-bin/{page}.ha")

    def get_cgi_page(self, page, *, auth=True):
        if page == "dosprotect":
            return HttpResponse(200, "OK", {}, self.body, "https://router.local/cgi-bin/dosprotect.ha")
        return super(_UnreadableVerificationRouter, self).get_cgi_page(page, auth=auth)


def _autorestore_with_unanswered_save(tmp_path, body):
    dump = replace(
        make_dump(), services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")], forwards=[], reservations=[],
        forms={"dosprotect": {"flag": UNCHECKED}},
    )
    reset = reset_pages()
    reset["dosprotect"] = dosprotect_page(fields=[checkbox("flag", "on", checked=True)])
    router = _UnansweredDosprotectRouter(body)
    store = RecoveryCheckpoint("router.local", dump, None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset, reset)
    result = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=lambda _: None, log=lambda _: None, checkpoint=store,
    )
    return result, router, fetcher, store


def test_autorestore_ends_a_cut_first_read_save_like_a_please_wait_one(clock, tmp_path):
    cut, cut_router, cut_fetcher, cut_store = _autorestore_with_unanswered_save(tmp_path / "cut", CUT)
    wait, wait_router, wait_fetcher, wait_store = _autorestore_with_unanswered_save(tmp_path / "wait", PLEASE_WAIT)
    assert cut.status == "error" and cut.exit_code == 2, (cut.reason, cut.passes)
    assert cut.write_unanswered is True
    assert [p for p, _ in cut_router.posts] == ["services", "dosprotect"]
    assert (cut.status, cut.exit_code, cut.reason, cut.write_unanswered) == (
        wait.status, wait.exit_code, wait.reason, wait.write_unanswered
    )
    assert len(cut_fetcher.calls) == len(wait_fetcher.calls)
    assert [p for p, _ in cut_router.posts] == [p for p, _ in wait_router.posts]
    assert cut_store.failure_count() == wait_store.failure_count() == 1
    cut_step = [s for s in cut.passes[0]["steps"] if s["status"] == "failed"][0]
    wait_step = [s for s in wait.passes[0]["steps"] if s["status"] == "failed"][0]
    assert cut_step == wait_step and cut_step["writeResponseReceived"] is True
