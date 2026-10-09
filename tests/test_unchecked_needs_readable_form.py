"""An absent checkbox on the post-save read counts as unchecked only when that page was a readable form.

A control-less "Please wait" page, a truncated page or a Login page shows no checkbox because it shows no
form at all; reading that as "the box is off" would verify a write nobody observed. The step then stays
acknowledged-but-unverifiable (exit 2, committed). A readable form that lacks the box (the gateway dropped
the control) still reads as unchecked, and one that still shows the box checked is still a mismatch."""

from __future__ import annotations

import json

import pytest
from save_helpers import SAVED_RED, client_with, html

from bgwcli import cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.errors import SnapshotExtractionError
from bgwcli.restore import RestorePostcondition, RestoreStep, _postcondition_matches
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"
PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
# Tables nested past the parser's bound: the page is cut off before it can show the box.
TRUNCATED = "<table>" * 300


def box_form(checked: bool | None, banner: str = "") -> str:
    """The form with the `flag` checkbox checked, unchecked, or not rendered at all (None)."""
    box = "" if checked is None else f'<input type="checkbox" name="flag" value="on"{" checked" if checked else ""}>'
    return (
        f'{banner}<form action="/cgi-bin/{PAGE}.ha"><input name="nonce" value="abc123">{box}'
        '<input type="text" name="other" value="x"><input type="submit" name="Save" value="Save"></form>'
    )


def _restore(tmp_env, capsys, monkeypatch, ack_body, rereads):
    """`restore --commit` turning `flag` off: two reads (live state, nonce), one POST answered by `ack_body`,
    then one scripted body per verification re-read (the last one repeats)."""
    dump = tmp_env / "dump.json"
    write_dump_file(dump, Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"flag": "<unchecked>"}}))
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(ack_body)
        if not state["posted"]:
            return html(box_form(True))
        reread = rereads.pop(0) if len(rereads) > 1 else rereads[0]
        return html(reread)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(dump), "--include", PAGE, "--commit", "--confirm", "RESTORE", "--json"])
    output = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha") for r in wire.requests)
    return code, output, output["execution"]["steps"][0], posts


def test_control_less_follow_up_read_does_not_prove_the_box_is_off(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    # The acknowledgement page still shows the box checked; the follow-up reads show no form at all.
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [PLEASE_WAIT])

    assert code == 2
    assert step["status"] == "failed"
    assert step["acknowledgementObserved"] is True and step["writePerformed"] is True
    assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
    assert "stateObserved" not in step
    assert "requested state could not be read" in step["error"] and "verify" in step["error"]
    assert posts == 1


@pytest.mark.parametrize(
    ("body", "state_observed"), [(TRUNCATED, "absent"), (LOGIN, False)], ids=["truncated", "login"]
)
def test_truncated_or_login_follow_up_read_is_never_applied(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, body, state_observed
):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [body])

    assert code == 2
    assert step["status"] == "failed"
    # A truncated read leaves the key out; a Login-page read (a lost session) reports it as False.
    assert step.get("stateObserved", "absent") == state_observed
    assert "verify the gateway state before retrying" in step["error"]
    assert posts == 1


def test_readable_form_with_the_box_absent_is_unchecked(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(None, SAVED_RED), [box_form(None)])
    assert code == 0 and step["status"] == "applied"
    assert step["stateObserved"] is True and step["acknowledgementObserved"] is True
    assert posts == 1


def test_readable_form_with_the_box_unchecked_is_applied(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(False, SAVED_RED), [box_form(False)])
    assert code == 0 and step["status"] == "applied" and step["stateObserved"] is True
    assert posts == 1


def test_readable_form_with_the_box_still_checked_is_a_mismatch(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, _, step, posts = _restore(tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [box_form(True)])
    assert code == 1 and step["status"] == "failed"
    assert step["acknowledgementObserved"] is True and step["stateObserved"] is False
    assert "state is not visible" in step["error"]
    assert posts == 1


NOT_FOUND = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"


def _unchecked_step() -> RestoreStep:
    return RestoreStep(
        1, "form", PAGE, "save", raw_payload={"Save": "Save"},
        postcondition=RestorePostcondition(unchecked_fields=("flag",)),
    )


@pytest.mark.parametrize(
    "body", [LOGIN, NOT_FOUND, TRUNCATED, PLEASE_WAIT], ids=["login", "not-found", "truncated", "control-less"]
)
def test_postcondition_on_a_non_form_page_is_unreadable_not_unchecked(body):
    with pytest.raises(SnapshotExtractionError):
        _postcondition_matches(_unchecked_step(), body)


def test_postcondition_on_a_readable_form_without_the_box_is_unchecked():
    assert _postcondition_matches(_unchecked_step(), box_form(None)) is True


def test_unreadable_read_then_readable_unchecked_form_still_verifies(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, _, step, posts = _restore(
        tmp_env, capsys, monkeypatch, box_form(True, SAVED_RED), [PLEASE_WAIT, box_form(None)]
    )
    assert code == 0 and step["status"] == "applied" and step["stateObserved"] is True
    assert posts == 1


def _unreadable_until_the_deadline_then_readable(clock, monkeypatch, capsys, argv):
    """Every read during the verification window is a control-less page; the read after the deadline (the
    closing snapshot, or the live re-read of `set`) is the readable form with the box off."""
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(box_form(True, SAVED_RED))
        if not state["posted"]:
            return html(box_form(True))
        return html(PLEASE_WAIT if clock.now < 3.0 else box_form(None))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    output = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha") for r in wire.requests)
    return code, output, posts


def test_acknowledged_save_unverifiable_until_the_deadline_is_exit_2_even_when_the_closing_snapshot_reads(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    dump = tmp_env / "dump.json"
    write_dump_file(dump, Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"flag": "<unchecked>"}}))
    code, output, posts = _unreadable_until_the_deadline_then_readable(
        clock, monkeypatch, capsys,
        ["restore", str(dump), "--include", PAGE, "--commit", "--confirm", "RESTORE"],
    )
    step = output["execution"]["steps"][0]
    assert step["status"] == "failed" and step["errorType"] == "SnapshotExtractionError"
    assert step["acknowledgementObserved"] is True and step["writePerformed"] is True
    assert step["writeAttempted"] is True and step["writeResponseReceived"] is True
    assert step.get("stateObserved") is not True
    assert "requested state could not be read" in step["error"] and "verify" in step["error"]
    assert output["diff"] is not None  # the closing snapshot was readable
    assert code == 2
    assert posts == 1


def test_set_with_an_acknowledged_save_unverifiable_until_the_deadline_still_exits_2(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, output, posts = _unreadable_until_the_deadline_then_readable(
        clock, monkeypatch, capsys, ["set", PAGE, "flag=off", "--commit", "--confirm", "DOSPROTECT"],
    )
    assert code == 2, output
    assert posts == 1
