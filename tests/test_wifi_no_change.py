"""Explicit Wi-Fi no-write notifications must not become saved-write claims."""

import json

import pytest
from test_client import html
from test_review_save_contracts import client_with, form, plan
from test_save_confirmation import SAVED_RED
from test_save_confirmation import clock as confirmation_clock

from bgwcli import cli
from bgwcli.restore import RestorePostcondition, RestoreStep, execute_restore

clock = confirmation_clock
NO_CHANGE = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">No changes detected. Save not performed.</div>'
)


@pytest.mark.parametrize("page", ["wconfig", "wconfig_unified"])
@pytest.mark.parametrize("redirect", [False, True])
def test_wifi_known_state_no_change_is_not_a_write_or_ack(clock, page, redirect):
    def handle(request, number):
        if number == 1:
            return html(form(page, "old"))
        if number == 2 and redirect:
            return html("", status=302, headers={"location": f"/cgi-bin/{page}.ha"})
        return html(form(page, "new", banner=NO_CHANGE))

    client, wire = client_with(handle)
    result = execute_restore(client, [RestoreStep(
        1, "form", page, "Save Wi-Fi", raw_payload={"setting": "new", "Save": "Save"},
        postcondition=RestorePostcondition(form_fields=("setting",)),
    )]).steps[0]
    assert result.status == "unchanged"
    assert result.write_attempted is True and result.write_performed is False
    assert result.acknowledgement_observed is False and result.state_observed is True
    assert not clock.sleeps
    assert [r.method for r in wire.requests] == (["GET", "POST", "GET"] if redirect else ["GET", "POST"])


@pytest.mark.parametrize("body", [form("wconfig", "old", banner=NO_CHANGE), NO_CHANGE])
def test_no_change_with_mismatched_or_unreadable_target_stops_without_repost(clock, body):
    client, wire = client_with(lambda request, n: html(form("wconfig", "old") if n == 1 else body))
    execution = execute_restore(client, plan("wconfig"))
    result = execution.steps[0]
    assert result.status == "failed" and execution.stopped_at == 1
    assert result.write_performed is False and result.write_attempted is True
    assert result.state_observed is not True
    assert "requested state" in result.error
    assert not clock.sleeps
    assert [r.method for r in wire.requests] == ["GET", "POST"]


@pytest.mark.parametrize("page", ["dosprotect", "services", "apphosting", "wmacauth", "ipalloc"])
def test_no_change_on_any_page_is_never_an_acknowledgement(clock, page):
    from bgwcli.restore import RestoreStep

    client, wire = client_with(lambda request, n: html(form(page, "same") if n == 1 else NO_CHANGE))
    result = execute_restore(client, [RestoreStep(1, "form", page, "Save", raw_payload={"Save": "Save"})]).steps[0]
    assert result.status == "unchanged"
    assert result.acknowledgement_observed is False and result.write_performed is False
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("operation", ["set", "submit-known", "submit"])
@pytest.mark.parametrize("matching", [True, False])
def test_cli_wifi_no_change_reports_no_commit_and_requires_known_state(
    clock, tmp_env, capsys, monkeypatch, operation, matching
):
    client, wire = client_with(lambda request, n: html(
        form("wconfig", "old") if n <= 2 else form("wconfig", "new" if matching else "old", banner=NO_CHANGE)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    args = ["set", "wconfig", "setting=new"] if operation == "set" else ["submit", "wconfig", "Save"]
    if operation == "submit-known":
        args.append("setting=new")
    code = cli.main([*args, "--commit", "--confirm", "WCONFIG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert output["committed"] is False
    assert output["writeAttempted"] is True and output["writePerformed"] is False
    assert output["acknowledgementObserved"] is False
    success = matching or operation == "submit"
    assert output["outcome"] == ("unchanged" if success else "failed")
    assert code == (0 if success else 1)  # live form differs from the request: a negative answer
    assert output.get("verified") == (None if operation == "submit" else matching)
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert not clock.sleeps


def test_cli_wifi_changes_saved_still_reports_committed(clock, tmp_env, capsys, monkeypatch):
    client, wire = client_with(lambda request, n: html(
        form("wconfig", "old") if n <= 2 else form("wconfig", "new", banner=SAVED_RED)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    assert cli.main(["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["committed"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.parametrize("confirmation", ["matching", "mismatch", "closing-failure"])
def test_autorestore_no_change_summary_convergence_and_recovery_intent(clock, tmp_path, recovering, confirmation):
    from urllib.parse import urlsplit

    from bgwcli.autorestore import AutorestoreOptions, run_autorestore
    from bgwcli.parser import parse_page
    from bgwcli.recovery_state import RecoveryCheckpoint
    from bgwcli.snapshot import extract_snapshot

    desired = extract_snapshot({"wconfig": parse_page("wconfig", form("wconfig", "new"))}, ts="", router_host="")
    posted = False

    def handle(request, number):
        nonlocal posted
        page = urlsplit(request.url).path.rsplit("/", 1)[-1].removesuffix(".ha")
        if request.method == "POST":
            posted = True
            return html(form(page, "old" if confirmation == "mismatch" else "new", banner=NO_CHANGE))
        if posted and confirmation == "closing-failure":
            raise TimeoutError("closing read unavailable")
        return html(form(page, "new" if posted and confirmation == "matching" else "old"))

    client, wire = client_with(handle)
    checkpoint = RecoveryCheckpoint(client.session_identity(), desired, ("wconfig",), root=tmp_path / "recovery")
    if recovering:
        checkpoint.begin()
    result = run_autorestore(
        lambda: (client, False), desired,
        AutorestoreOptions(commit=True, pages=("wconfig",), on_any_diff=True, max_passes=3),
        fetch_pages=cli._fetch_snapshot_pages, checkpoint=checkpoint, log=lambda _: None, sleep=lambda _: None,
    )
    expected = {"matching": "converged", "mismatch": "not-converged", "closing-failure": "error"}
    assert result.status == expected[confirmation]
    # A "No changes detected" answer that leaves the page in another state was a sent write: later
    # passes never re-post it (only failures before any final write may retry).
    expected_passes = 1
    assert len(result.passes) == expected_passes
    if confirmation == "mismatch":
        assert "the next timer run will re-read the gateway and retry" in result.reason
    assert result.passes[0]["applied"] == 0
    assert result.passes[0]["unchanged"] == (0 if confirmation == "mismatch" else 1)
    # The Save was sent, so the intent (and the failure count riding on it) outlives a run that did
    # not converge, whether or not the answer was "No changes detected".
    assert checkpoint.is_active() is (confirmation != "matching")
    assert sum(r.method == "POST" for r in wire.requests) == expected_passes
    assert not clock.sleeps


@pytest.mark.parametrize("body", [
    '<script>var message = "No changes detected. Save not performed.";</script>',
    '<p>No changes detected. Save not performed.</p>',
    '<div id="error-message-text">No changes detected. Save not performed. Invalid input</div>',
])
def test_wifi_no_change_requires_exact_rendered_notification(clock, body):
    client, wire = client_with(lambda request, n: html(form("wconfig", "old") if n == 1 else body))
    result = execute_restore(client, plan("wconfig")).steps[0]
    assert result.status == "failed"
    assert result.acknowledgement_observed is False
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("failure", ["pool", "auth"])
def test_wifi_cli_preserves_confirmation_coordination_errors(clock, tmp_env, capsys, monkeypatch, failure):
    from bgwcli.client import session_pool_full_error
    from bgwcli.errors import RouterAuthError

    def handle(request, number):
        if number <= 2:
            return html(form("wconfig", "old"))
        if number == 3:
            return html("", status=302, headers={"location": "/cgi-bin/wconfig.ha"})
        if failure == "pool":
            raise session_pool_full_error(waited_ms=2300, retry_count=4)
        raise RouterAuthError("authentication failed")

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG", "--json"])
    captured = capsys.readouterr()
    assert code == 2
    if failure == "pool":
        output = json.loads(captured.out)
        assert output["sessionPoolFull"] is True
        assert output["waitedMs"] == 2300 and output["retryCount"] == 4
    else:
        assert "authentication failed" in captured.err
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("matching", [True, False])
def test_cli_restore_no_save_does_not_claim_committed(clock, tmp_env, tmp_path, capsys, monkeypatch, matching):
    from bgwcli.dumpfile import write_dump_file
    from bgwcli.parser import parse_page
    from bgwcli.snapshot import extract_snapshot

    dump = extract_snapshot({"wconfig": parse_page("wconfig", form("wconfig", "new"))}, ts="", router_host="")
    path = tmp_path / "wifi.json"
    write_dump_file(path, dump)
    client, wire = client_with(lambda request, n: html(
        form("wconfig", "old") if n <= 2 else form("wconfig", "new" if matching else "old", banner=NO_CHANGE)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(path), "--include", "wconfig", "--commit", "--confirm", "RESTORE", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == (0 if matching else 1)
    assert output["operation"]["committed"] is False
    assert output["operation"]["writePerformed"] is False
    assert output["execution"]["steps"][0]["status"] == ("unchanged" if matching else "failed")
    if matching:
        assert "1 unchanged" in output["operation"]["result"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("unavailable", ["missing-form", "transport"])
def test_cli_no_change_requires_readable_requested_state(clock, tmp_env, capsys, monkeypatch, unavailable):
    def handle(request, number):
        if number <= 2:
            return html(form("wconfig", "old"))
        if number == 3:
            return html(NO_CHANGE)
        if unavailable == "transport":
            raise TimeoutError("verification unavailable")
        return html("<html><body>No configuration form</body></html>")

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG", "--json"])
    output = json.loads(capsys.readouterr().out)
    # An unreadable re-read (transport failure, or a page without any form control) is "could not
    # answer" (2), never a difference against an invented "<absent>" live value.
    assert code == 2 and output["committed"] is False
    assert output["outcome"] == "failed" and output["writePerformed"] is False
    assert output.get("verified") is not True and output.get("mismatches") is None
    assert "<absent>" not in json.dumps(output)
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert not clock.sleeps
