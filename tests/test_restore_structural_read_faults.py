"""A structural read failure during restore keeps its exception type on the step.

An unreadable apphosting re-read for a deferred forward (before that step's POST) and an unreadable
post-save verification page at the deadline (after an acknowledged save) are both "no answer": restore
exits 2 even when the closing snapshot reads, and autorestore ends the run instead of replanning or
reporting an ordinary non-convergence.
"""

from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qsl

from integration_html import (
    APPHOSTING_WITH_STAR_MOSH_HTML,
    APPHOSTING_WITHOUT_MOSH_HTML,
    CHANGES_SAVED_HTML,
    RESTORE_SERVICES_HTML,
    saved_configuration_html,
)
from page_builders import apphosting_page, checkbox, dosprotect_page
from save_helpers import client_with, html
from test_autorestore import FakeRouter, Fetcher, full_pages, make_dump, reset_pages

from bgwcli import cli
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.dumpfile import write_dump_file
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import UNCHECKED, Snapshot, SnapshotForward, SnapshotMeta, SnapshotService
from bgwcli.types import HttpResponse

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
MOSH = SnapshotService("Mosh", 60001, 60010, 60001, "UDP")
FWD_MOSH = SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")
LIVE_SERVICES = RESTORE_SERVICES_HTML.replace("custom_ssh", "other_ssh").replace('value="n"', 'value="abc123"')


def _deferred_forward_restore(tmp_env, capsys, monkeypatch):
    """`restore --commit` of one service plus its forward: the service add is applied, the apphosting
    re-read for the deferred forward is a Please-wait page, and every later apphosting read is readable."""
    dump = tmp_env / "dump.json"
    write_dump_file(
        dump, Snapshot(SnapshotMeta("", "", "router.local"), services=[MOSH], forwards=[FWD_MOSH]),
    )
    state = {"services_posted": False, "apphosting_reads_after": 0}

    def handle(request, n):
        page = request.url.split("?")[0].rsplit("/", 1)[-1].removesuffix(".ha")
        if request.method == "POST":
            state["services_posted"] = page == "services" or state["services_posted"]
            fields = dict(parse_qsl((request.body or b"").decode()))
            return html(saved_configuration_html(page, fields) if page == "services" else CHANGES_SAVED_HTML)
        if page == "apphosting":
            if not state["services_posted"]:
                return html(APPHOSTING_WITHOUT_MOSH_HTML)
            state["apphosting_reads_after"] += 1
            return html(PLEASE_WAIT if state["apphosting_reads_after"] == 1 else APPHOSTING_WITH_STAR_MOSH_HTML)
        return html(LIVE_SERVICES)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(dump), "--include", "services,apphosting", "--commit", "--confirm", "RESTORE",
                     "--json"])
    output = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha")]
    return code, output, posts


def test_an_unreadable_deferred_forward_re_read_exits_2_even_when_the_closing_snapshot_reads(
    clock, tmp_env, capsys, monkeypatch
):
    code, output, posts = _deferred_forward_restore(tmp_env, capsys, monkeypatch)
    failed = [s for s in output["execution"]["steps"] if s["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["page"] == "apphosting"
    assert failed[0]["errorType"] == "SnapshotExtractionError"
    assert failed[0]["writeAttempted"] is False
    assert "re-reading apphosting" in failed[0]["error"]
    assert output["diff"] is not None  # the closing snapshot was readable
    assert code == 2
    assert len(posts) == 1  # only the service add was sent


class _UnreadableVerificationRouter(FakeRouter):
    """The Save is acknowledged ("Changes saved", no form on the page) and every verification read of the
    page is a control-less Please-wait page."""

    def post_cgi_page(self, page, fields):
        if page != "dosprotect":
            return super().post_cgi_page(page, fields)
        self.posts.append((page, dict(fields)))
        return HttpResponse(200, "OK", {}, CHANGES_SAVED_HTML, f"https://router.local/cgi-bin/{page}.ha")

    def get_cgi_page(self, page, *, auth=True):
        if page == "dosprotect":
            return HttpResponse(200, "OK", {}, PLEASE_WAIT, "https://router.local/cgi-bin/dosprotect.ha")
        return super().get_cgi_page(page, auth=auth)


def test_autorestore_ends_the_run_when_an_acknowledged_save_cannot_be_verified(clock, tmp_path):
    # One service the reset router lacks (so a reset is detected) and a checkbox that must end unchecked.
    dump = replace(
        make_dump(), services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")], forwards=[], reservations=[],
        forms={"dosprotect": {"flag": UNCHECKED}},
    )
    reset = reset_pages()
    reset["dosprotect"] = dosprotect_page(fields=[checkbox("flag", "on", checked=True)])
    # A readable closing page would be served if the run read again (it must not).
    readable = reset_pages()
    readable["dosprotect"] = dosprotect_page(fields=[checkbox("flag", "on", checked=False)])
    router = _UnreadableVerificationRouter()
    store = RecoveryCheckpoint("router.local", dump, None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset, readable)
    sleeps: list[float] = []
    result = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2, (result.reason, result.passes)
    assert "verification-unavailable" in result.reason and "failure 1/3" in result.reason
    assert len(result.passes) == 1 and sleeps == []
    assert len(fetcher.calls) == 1  # the opening read only: no closing read after the acknowledged save
    assert [page for page, _ in router.posts] == ["services", "dosprotect"]
    assert store.is_active() and store.failure_count() == 1
    step = [s for s in result.passes[0]["steps"] if s["status"] == "failed"][0]
    assert step["errorType"] == "SnapshotExtractionError" and step["acknowledgementObserved"] is True
    assert step["writePerformed"] is True and step["writeAttempted"] is True


class _PleaseWaitApphostingRouter(FakeRouter):
    def get_cgi_page(self, page, *, auth=True):
        if page == "apphosting":
            return HttpResponse(200, "OK", {}, PLEASE_WAIT, "https://router.local/cgi-bin/apphosting.ha")
        return super().get_cgi_page(page, auth=auth)


def test_autorestore_ends_the_run_on_an_unreadable_deferred_forward_re_read(tmp_path):
    router = _PleaseWaitApphostingRouter()
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    reset = reset_pages()
    reset["apphosting"] = apphosting_page(rows=[], service_options=[])  # the dropdown lacks the added services
    fetcher = Fetcher(reset, full_pages())
    sleeps: list[float] = []
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2, (result.reason, router.posts, result.passes)
    assert "failure 1/3" in result.reason and "no further pass in this run" in result.reason
    assert "the apphosting page could not be read before the write was sent" in result.reason
    assert "SnapshotExtractionError" not in result.reason
    assert len(result.passes) == 1 and sleeps == []
    assert len(fetcher.calls) == 1  # the opening read only: no replanning, no closing read
    assert [page for page, _ in router.posts] == ["services", "services"]  # nothing posted after the fault
    assert store.is_active() and store.failure_count() == 1
    step = [s for s in result.passes[0]["steps"] if s["status"] == "failed"][0]
    assert step["errorType"] == "SnapshotExtractionError" and step["writeAttempted"] is False
