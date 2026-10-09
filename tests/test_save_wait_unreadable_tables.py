"""An acknowledged save whose verification page cannot be read is unverifiable for every postcondition kind.

`services`, `apphosting` and `ipalloc` are table pages, not form pages: a "Please wait" document or a page
with no table header extracts as an EMPTY table, so without a readability gate the wait answered "the row is
absent" (a false `stateObserved: false`, exit 1) where it knows nothing. Every kind (service add/remove,
forward add/remove, reservation) now treats such a poll as unreadable: exit 2, the structural error type, the
acknowledgement and performed evidence kept, `stateObserved` absent, one write POST. A readable page that
lacks the row stays a readable negative (exit 1); one that shows it is applied.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

import pytest
from integration_html import (
    CHANGES_SAVED_HTML,
    IPALLOC_HTML,
    RESTORE_APPHOSTING_HTML,
    RESTORE_SERVICES_HTML,
    STALE_ROW,
    allocation_saved_html,
    entry_page_html,
    gateway_empty_page,
)
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.errors import SnapshotExtractionError
from bgwcli.restore import RestorePostcondition, RestoreStep, _postcondition_matches
from bgwcli.snapshot import Snapshot, SnapshotForward, SnapshotMeta, SnapshotReservation, SnapshotService

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
WRONG_HEADER = "<html><body><table><tr><th>x</th></tr><tr><td>y</td></tr></table></body></html>"
CUT = "<html><body>" + "<p>x</p>" * 160000 + "</body></html>"
NO_HEADER = (
    '<html><body><form method="post"><input type="hidden" name="nonce" value="n">'
    '<input type="text" name="Service" value=""></form></body></html>'
)
MOSH = SnapshotService("Mosh", 60001, 60010, 60001, "UDP")
MAC = "02:0a:0b:0c:0d:04"
IP = "192.168.1.67"
MOSH_SERVICE_ROW = (
    '<tr><td>Mosh</td><td>60001-60010</td><td>60001</td><td>UDP</td>'
    '<td><input type="submit" name="Remove_9" value="Remove"></td></tr>'
)
MOSH_FORWARD_ROW = '<tr><td>Mosh</td><td>host-a</td><td><input type="submit" name="Remove_9" value="Remove"></td></tr>'
CUSTOM_SSH_SERVICE_ROW = (
    '<tr><td>custom_ssh</td><td>2483-2483</td><td>22</td><td>TCP</td>'
    '<td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
)
CUSTOM_SSH_FORWARD_ROW = (
    '<tr><td>custom_ssh</td><td>host-b</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
)


@dataclass(frozen=True)
class Scenario:
    snapshot: Snapshot
    argv: tuple[str, ...]
    page: str
    live: str  # the page before the write
    lacks: str  # a readable page after the save that does not show the requested state
    holds: str  # a readable page after the save that shows it
    posts: int  # all POSTs of a successful run (the reservation opens its entry editor first)


def _meta() -> SnapshotMeta:
    return SnapshotMeta("", "", "router.local")


SCENARIOS = {
    "service-add": Scenario(
        Snapshot(_meta(), services=[MOSH]), ("--include", "services"), "services",
        RESTORE_SERVICES_HTML, RESTORE_SERVICES_HTML,
        RESTORE_SERVICES_HTML.replace(STALE_ROW, MOSH_SERVICE_ROW + STALE_ROW), 1,
    ),
    "service-remove": Scenario(
        Snapshot(_meta(), services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")]),
        ("--include", "services", "--prune"), "services",
        RESTORE_SERVICES_HTML, RESTORE_SERVICES_HTML, RESTORE_SERVICES_HTML.replace(STALE_ROW, ""), 1,
    ),
    "forward-add": Scenario(
        Snapshot(_meta(), forwards=[SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")]),
        ("--include", "apphosting"), "apphosting",
        RESTORE_APPHOSTING_HTML, RESTORE_APPHOSTING_HTML,
        RESTORE_APPHOSTING_HTML.replace("</table>", MOSH_FORWARD_ROW + "</table>"), 1,
    ),
    "forward-remove": Scenario(
        Snapshot(_meta(), forwards=[]), ("--include", "apphosting", "--prune"), "apphosting",
        RESTORE_APPHOSTING_HTML, RESTORE_APPHOSTING_HTML, RESTORE_APPHOSTING_HTML.replace(CUSTOM_SSH_FORWARD_ROW, ""),
        1,
    ),
    "reserve": Scenario(
        Snapshot(_meta(), reservations=[SnapshotReservation(MAC, IP)]), ("--include", "ipalloc"), "ipalloc",
        IPALLOC_HTML, IPALLOC_HTML, allocation_saved_html(MAC, IP), 2,
    ),
}
POLLS = {"wait": PLEASE_WAIT, "wrong-header": WRONG_HEADER, "no-header": NO_HEADER, "cut": CUT}
# Two writes in one run (kept out of SCENARIOS, whose tests assume a single write): the router's only
# service is Stale with the wrong ports, so --prune removes it (the re-read is then the gateway's empty
# page) and the dump's Stale is added right after.
_ONLY_STALE = RESTORE_SERVICES_HTML.replace(CUSTOM_SSH_SERVICE_ROW, "")
SERVICE_REPLACE = Scenario(
    Snapshot(_meta(), services=[SnapshotService("Stale", 9, 9, 9, "TCP")]), ("--include", "services", "--prune"),
    "services", _ONLY_STALE, _ONLY_STALE,
    _ONLY_STALE.replace(STALE_ROW, STALE_ROW.replace("1-1", "9-9").replace("<td>1</td>", "<td>9</td>")), 2,
)


def _run(tmp_env, capsys, monkeypatch, name, polls, *, extra_argv=()):
    """`restore --commit` of one scenario: the write POST is answered by a bare "Changes saved" banner and every
    verification read serves the scripted bodies (the last repeats)."""
    scenario = SCENARIOS[name] if isinstance(name, str) else name
    dump = tmp_env / "dump.json"
    write_dump_file(dump, scenario.snapshot)
    # a poll is a named body above, or (anything else) the page body itself
    bodies = [{**POLLS, "lacks": scenario.lacks, "holds": scenario.holds}.get(p, p) for p in polls]
    state = {"allocated": False, "saved": False}

    def serve(body):  # the write gate wants a plausible nonce, as in the sibling tests
        return html(body.replace('value="n"', 'value="abc123"'))

    def handle(request, n):
        page = request.url.split("?")[0].rsplit("/", 1)[-1].removesuffix(".ha")
        if request.method == "POST":
            fields = dict(parse_qsl((request.body or b"").decode()))
            if any(key.startswith("Allocate_") for key in fields):
                state["allocated"] = True
                return serve(entry_page_html(MAC, [IP]))
            state["saved"] = True
            return serve(CHANGES_SAVED_HTML)
        if page != scenario.page:  # a companion read (a service removal also reads the forwards)
            return serve({"apphosting": RESTORE_APPHOSTING_HTML, "services": RESTORE_SERVICES_HTML}.get(
                page, IPALLOC_HTML))
        if state["saved"]:
            return serve(bodies.pop(0) if len(bodies) > 1 else bodies[0])
        if state["allocated"]:
            return serve(entry_page_html(MAC, [IP]))
        return serve(scenario.live)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(dump), *scenario.argv, "--commit", "--confirm", "RESTORE", "--json", *extra_argv])
    output = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha")]
    return code, output, posts


def _write_step(output):
    return [s for s in output["execution"]["steps"] if s["status"] != "not-run"][-1]


@pytest.mark.parametrize("name", list(SCENARIOS))
@pytest.mark.parametrize("poll", list(POLLS))
def test_acknowledged_save_with_unreadable_polls_is_unverifiable_not_absent(
    clock, tmp_env, capsys, monkeypatch, name, poll
):
    code, output, posts = _run(tmp_env, capsys, monkeypatch, name, [poll])
    step = _write_step(output)
    assert step["status"] == "failed"
    assert step["errorType"] == "SnapshotExtractionError"
    assert step["acknowledgementObserved"] is True and step["writePerformed"] is True
    assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
    assert "stateObserved" not in step
    assert "requested state could not be read" in step["error"]
    assert code == 2
    assert len(posts) == SCENARIOS[name].posts


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_acknowledged_save_with_a_readable_page_lacking_the_row_is_a_readable_negative(
    clock, tmp_env, capsys, monkeypatch, name
):
    code, output, posts = _run(tmp_env, capsys, monkeypatch, name, ["lacks"])
    step = _write_step(output)
    assert step["status"] == "failed" and step["stateObserved"] is False
    assert step["acknowledgementObserved"] is True
    assert "errorType" not in step
    assert "state is not visible" in step["error"]
    assert code == 1
    assert len(posts) == SCENARIOS[name].posts


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_acknowledged_save_with_a_readable_page_showing_the_row_is_applied(
    clock, tmp_env, capsys, monkeypatch, name
):
    code, output, posts = _run(tmp_env, capsys, monkeypatch, name, ["holds"])
    step = _write_step(output)
    assert step["status"] == "applied" and step["stateObserved"] is True
    assert code == 0
    assert len(posts) == SCENARIOS[name].posts


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_unreadable_then_readable_polls_still_verify(clock, tmp_env, capsys, monkeypatch, name):
    code, output, posts = _run(tmp_env, capsys, monkeypatch, name, ["wait", "wrong-header", "holds"])
    step = _write_step(output)
    assert step["status"] == "applied" and step["stateObserved"] is True
    assert code == 0 and len(posts) == SCENARIOS[name].posts


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_readable_negative_then_unreadable_polls_is_unverifiable(clock, tmp_env, capsys, monkeypatch, name):
    code, output, _ = _run(tmp_env, capsys, monkeypatch, name, ["lacks", "wait"])
    step = _write_step(output)
    assert step["errorType"] == "SnapshotExtractionError" and "stateObserved" not in step
    assert code == 2


def _postcondition_step(kind: str) -> RestoreStep:
    if kind == "add-service":
        return RestoreStep(1, kind, "services", "add", postcondition=RestorePostcondition(service=MOSH))
    if kind == "remove-service":
        return RestoreStep(1, kind, "services", "remove", postcondition=RestorePostcondition(absent_row=("Stale", "")))
    if kind == "add-forward":
        wanted = SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")
        return RestoreStep(1, kind, "apphosting", "add", postcondition=RestorePostcondition(forward=wanted))
    return RestoreStep(
        1, "remove-forward", "apphosting", "remove",
        postcondition=RestorePostcondition(absent_row=("custom_ssh", "host-b")),
    )


@pytest.mark.parametrize("kind", ["add-service", "remove-service", "add-forward", "remove-forward"])
@pytest.mark.parametrize(
    "body", [PLEASE_WAIT, WRONG_HEADER, NO_HEADER, CUT], ids=["wait", "wrong-header", "no-header", "cut"]
)
def test_postcondition_on_an_unreadable_table_page_raises_instead_of_answering_absent(kind, body):
    with pytest.raises(SnapshotExtractionError):
        _postcondition_matches(_postcondition_step(kind), body)


@pytest.mark.parametrize("kind,page", [("remove-service", "services"), ("remove-forward", "apphosting")])
def test_postcondition_of_the_last_remove_reads_the_gateways_empty_page_as_absent(kind, page):
    # Removing the last row leaves the gateway's one-cell "No ... entries have been defined" table (no
    # header row): the row is gone, a readable positive, never an unreadable verification.
    assert _postcondition_matches(_postcondition_step(kind), gateway_empty_page(page)) is True
    # Another section's empty sentence proves nothing about this page.
    other = "apphosting" if page == "services" else "services"
    with pytest.raises(SnapshotExtractionError):
        _postcondition_matches(_postcondition_step(kind), gateway_empty_page(page, marker_page=other))


def test_last_prune_remove_is_applied_when_the_re_read_is_the_gateways_empty_page(tmp_env, capsys, monkeypatch, clock):
    # forward-remove: the dump has no forwards, the router one; after the Remove the gateway answers its
    # empty page. The step is applied with the state observed, one POST, and the closing diff converges.
    code, output, posts = _run(tmp_env, capsys, monkeypatch, "forward-remove", [gateway_empty_page("apphosting")])
    step = _write_step(output)
    assert (code, step["status"], step["stateObserved"], len(posts)) == (0, "applied", True, 1), output


def test_replacing_the_only_service_removes_through_the_gateways_empty_page_and_adds(
    tmp_env, capsys, monkeypatch, clock
):
    # Remove -> re-read is the empty page (row gone) -> Add -> re-read shows the dump's row: two POSTs,
    # both steps applied, exit 0. Before the empty page was readable the run stopped after the Remove.
    code, output, posts = _run(
        tmp_env, capsys, monkeypatch, SERVICE_REPLACE, [gateway_empty_page("services"), SERVICE_REPLACE.holds],
    )
    steps = [
        (s["kind"], s["status"], s.get("stateObserved")) for s in output["execution"]["steps"] if s["kind"] != "skip"
    ]
    assert (code, len(posts)) == (0, 2), output
    assert steps == [("remove-service", "applied", True), ("add-service", "applied", True)], output


def test_postcondition_on_a_readable_table_page_still_answers_yes_or_no():
    assert _postcondition_matches(_postcondition_step("add-service"), RESTORE_SERVICES_HTML) is False
    present = SCENARIOS["service-add"].holds
    assert _postcondition_matches(_postcondition_step("add-service"), present) is True
    assert _postcondition_matches(_postcondition_step("remove-service"), RESTORE_SERVICES_HTML) is False
    gone = SCENARIOS["service-remove"].holds
    assert _postcondition_matches(_postcondition_step("remove-service"), gone) is True


# --- a services page whose header lacks a column the removal check needs (here: a renamed Protocol header) --------
# With no data row left, the snapshot reader needs no further column, so the page passes the readability gate
# (the name column is there); it still cannot prove a removed row is gone.

NO_PROTOCOL_COLUMN = re.sub(r"<tr><td>.*?</tr>", "", RESTORE_SERVICES_HTML, flags=re.DOTALL).replace(
    "<th>Protocol</th>", "<th>Prtcl</th>"
)


def test_header_renamed_services_page_is_header_only_with_a_renamed_protocol_column():
    assert "Prtcl" in NO_PROTOCOL_COLUMN and "<td>" not in NO_PROTOCOL_COLUMN


def test_remove_service_postcondition_on_a_table_lacking_a_needed_column_raises():
    with pytest.raises(SnapshotExtractionError, match="shows no custom services table with the columns needed"):
        _postcondition_matches(_postcondition_step("remove-service"), NO_PROTOCOL_COLUMN)


def test_remove_service_with_a_header_renamed_page_to_the_deadline_is_unverifiable_not_still_present(
    clock, tmp_env, capsys, monkeypatch
):
    code, output, posts = _run(tmp_env, capsys, monkeypatch, "service-remove", [NO_PROTOCOL_COLUMN])
    step = _write_step(output)
    assert step["status"] == "failed"
    assert step["errorType"] == "SnapshotExtractionError"
    assert step["acknowledgementObserved"] is True and step["writePerformed"] is True
    assert "stateObserved" not in step
    assert "requested state could not be read" in step["error"]
    assert code == 2
    assert len(posts) == 1


# --- autorestore: an acknowledged save with an unreadable table page is verification-unavailable ------------------


def test_autorestore_ends_the_run_when_an_acknowledged_service_add_cannot_be_verified(clock, tmp_path):
    from dataclasses import replace

    from test_autorestore import FakeRouter, Fetcher, make_dump, reset_pages
    from test_restore_structural_read_faults import PLEASE_WAIT as WAIT

    from bgwcli.autorestore import AutorestoreOptions, run_autorestore
    from bgwcli.recovery_state import RecoveryCheckpoint
    from bgwcli.types import HttpResponse

    class UnreadableServicesRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            if page != "services":
                return super().post_cgi_page(page, fields)
            self.posts.append((page, dict(fields)))
            return HttpResponse(200, "OK", {}, CHANGES_SAVED_HTML, f"https://router.local/cgi-bin/{page}.ha")

        def get_cgi_page(self, page, *, auth=True):
            if page == "services" and self.posts:
                return HttpResponse(200, "OK", {}, WAIT, "https://router.local/cgi-bin/services.ha")
            return super().get_cgi_page(page, auth=auth)

    dump = replace(
        make_dump(), services=[SnapshotService("custom_ssh", 2483, 2483, 22, "TCP")], forwards=[], reservations=[],
        forms={},
    )
    router = UnreadableServicesRouter()
    store = RecoveryCheckpoint("router.local", dump, None, root=tmp_path / "recovery")
    fetcher = Fetcher(reset_pages(), reset_pages())
    sleeps: list[float] = []
    result = run_autorestore(
        lambda: (router, False), dump, AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2, (result.reason, result.passes)
    assert "verification-unavailable" in result.reason and "failure 1/3" in result.reason
    assert len(result.passes) == 1 and sleeps == []
    assert len(fetcher.calls) == 1  # the opening read only: no closing read after the acknowledged save
    assert [page for page, _ in router.posts] == ["services"]
    assert store.is_active() and store.failure_count() == 1
    step = [s for s in result.passes[0]["steps"] if s["status"] == "failed"][0]
    assert step["errorType"] == "SnapshotExtractionError" and step["acknowledgementObserved"] is True
    assert step["writePerformed"] is True and step["writeAttempted"] is True
