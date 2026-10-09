"""Save contract regressions through real parsers and the client's transport boundary."""

from dataclasses import replace

import pytest
from test_client import FakeTransport, html, make_client
from test_save_confirmation import ERROR, SAVED_RED
from test_save_confirmation import clock as confirmation_clock

from bgwcli import cli
from bgwcli.client import pool_full_metadata, session_pool_full_error
from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, build_restore_plan, execute_restore
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

clock = confirmation_clock


def form(page, value, name="setting", banner=""):
    return (
        f'{banner}<form action="/cgi-bin/{page}.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="{name}" value="{value}">'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def plan(page, name="setting"):
    parsed = {page: parse_page(page, form(page, "old", name), include_secrets=True)}
    current = extract_snapshot(parsed, ts="", router_host="", include=(page,))
    desired = replace(current, forms={page: {name: "192.0.2.1" if name == "ipaddr" else "new"}})
    return build_restore_plan(diff_snapshots(desired, current), desired, parsed, RestoreOptions(pages=(page,)))


def client_with(handler):
    transport = FakeTransport(handler)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "test"}})
    return client, transport


@pytest.mark.parametrize("page", ["etherlan", "ippass", "wmacauth", "dhcpserver"])
def test_optional_restore_page_observes_requested_state(clock, page):
    client, wire = client_with(
        lambda request, n: html(form(page, "old") if n == 1 else form(page, "new", banner=SAVED_RED))
    )
    result = execute_restore(client, plan(page)).steps[0]
    assert result.status == "applied"
    assert result.state_observed is True and result.acknowledgement_observed is True
    assert [r.method for r in wire.requests] == ["GET", "POST"]


def test_lan_post_with_ack_and_requested_state_is_applied_without_old_address_poll(clock):
    client, wire = client_with(
        lambda request, n: html(
            form("dhcpserver", "old", "ipaddr") if n == 1 else form("dhcpserver", "192.0.2.1", "ipaddr", SAVED_RED)
        )
    )
    result = execute_restore(client, plan("dhcpserver", "ipaddr")).steps[0]
    assert result.status == "applied"
    assert result.state_observed is True and result.acknowledgement_observed is True
    assert result.reconnect_address == "192.0.2.1"
    assert result.lan_address_changed is True
    assert [r.method for r in wire.requests] == ["GET", "POST"]


@pytest.mark.parametrize("outcome", ["pre-send", "rejected", "unknown", "applied"])
def test_cli_lan_reports_only_evidenced_commit(clock, tmp_env, capsys, monkeypatch, outcome):
    def handle(request, number):
        if number == 1:
            return html(form("dhcpserver", "192.0.2.254", "ipaddr"))
        if number == 2:
            if outcome == "pre-send":
                raise TimeoutError("nonce GET failed")
            return html(form("dhcpserver", "192.0.2.254", "ipaddr"))
        assert request.method == "POST", "LAN address move must not poll old address"
        if outcome == "unknown":
            raise TimeoutError("POST reply lost")
        if outcome == "rejected":
            return html(ERROR)
        return html(form("dhcpserver", "192.0.2.1", "ipaddr", SAVED_RED))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "dhcpserver", "ipaddr=192.0.2.1", "--commit", "--confirm", "DHCPSERVER", "--json"])
    import json

    output = json.loads(capsys.readouterr().out)
    assert output["committed"] is (outcome == "applied")
    assert output["writeAttempted"] is (outcome != "pre-send")
    assert (
        output["outcome"]
        == {"pre-send": "failed", "rejected": "failed", "unknown": "reconnect-required", "applied": "applied"}[outcome]
    )
    assert output.get("reconnectAddress") == ("192.0.2.1" if outcome in {"unknown", "applied"} else None)
    # Rejection is a definitive negative (1); pre-send and unknown outcomes are "could not answer" (2).
    assert code == {"pre-send": 2, "rejected": 1, "unknown": 2, "applied": 0}[outcome]
    assert sum(r.method == "POST" for r in wire.requests) == (0 if outcome == "pre-send" else 1)


# Helper-level checks of the amended message/metadata; the dispatch-level pool-full tests in
# tests/test_save_contract.py (through cli.main) are authoritative for exit code and JSON.
def test_cli_confirmation_preserves_pool_wait_metadata(clock):
    def handle(request, number):
        if number == 1:
            return html(form("wconfig", "old"))
        if number == 2:
            return html("", status=302, headers={"location": "/cgi-bin/wconfig.ha"})
        raise session_pool_full_error(waited_ms=4200, retry_count=7)

    client, wire = client_with(handle)
    with pytest.raises(type(session_pool_full_error())) as caught:
        cli._post_and_confirm(client, "wconfig", {"Save": "Save"})
    assert pool_full_metadata(caught.value) == (4200, 7)
    assert "sent once" in str(caught.value)
    assert [r.method for r in wire.requests] == ["GET", "POST", "GET"]


def test_set_verification_preserves_pool_wait_metadata(clock, verify_sleeps):
    from types import SimpleNamespace

    client, _ = client_with(
        lambda request, number: (_ for _ in ()).throw(session_pool_full_error(waited_ms=6500, retry_count=9))
    )
    with pytest.raises(type(session_pool_full_error())) as caught:
        cli._verify_set(
            client, "etherlan", SimpleNamespace(raw_payload={"setting": "new"}, display_changes={"setting": "new"})
        )
    assert pool_full_metadata(caught.value) == (6500, 9)
    assert verify_sleeps == []  # pool-full is never retried by the verification re-read


def test_rejected_lan_save_text_does_not_claim_committed(clock, tmp_env, capsys, monkeypatch):
    client, wire = client_with(
        lambda request, n: html(ERROR if request.method == "POST" else form("dhcpserver", "192.0.2.254", "ipaddr"))
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "dhcpserver", "ipaddr=192.0.2.1", "--commit", "--confirm", "DHCPSERVER"])
    output = capsys.readouterr().out
    assert code == 1  # an explicit rejection is a negative answer on every save path
    assert "set committed" not in output
    assert "router rejected" in output
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_cli_restore_confirmed_mask_only_save_still_computes_closing_diff(
    clock, tmp_env, tmp_path, capsys, monkeypatch
):
    import json

    from bgwcli.dumpfile import write_dump_file

    page = "dhcpserver"
    requested = form(page, "255.255.255.0", "ipmask", SAVED_RED)
    dump = extract_snapshot({page: parse_page(page, requested)}, ts="", router_host="", include=(page,))
    path = tmp_path / "lan.json"
    write_dump_file(path, dump)
    client, wire = client_with(lambda request, n: html(form(page, "255.255.0.0", "ipmask") if n <= 2 else requested))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(path), "--include", page, "--commit", "--confirm", "RESTORE", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["execution"]["steps"][0]["status"] == "applied"
    assert output["diff"] is not None
    assert [r.method for r in wire.requests] == ["GET", "GET", "POST", "GET"]


@pytest.mark.parametrize("failure_phase", ["confirmation", "closing"])
def test_cli_restore_reports_pool_wait_metadata(clock, tmp_env, tmp_path, capsys, monkeypatch, failure_phase):
    import json

    from bgwcli.dumpfile import write_dump_file

    page = "wconfig"
    requested = form(page, "new", banner=SAVED_RED)
    dump = extract_snapshot({page: parse_page(page, requested)}, ts="", router_host="")
    path = tmp_path / "wifi.json"
    write_dump_file(path, dump)

    def handle(request, number):
        if number <= 2:
            return html(form(page, "old"))
        if number == 3:
            return html(
                requested if failure_phase == "closing" else "", status=200 if failure_phase == "closing" else 302
            )
        if number == 4:
            raise session_pool_full_error(waited_ms=8400, retry_count=11)
        return html(requested)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["restore", str(path), "--include", page, "--commit", "--confirm", "RESTORE", "--json"])
    output = json.loads(capsys.readouterr().out)
    # A full pool while the write's acknowledgement was awaited leaves the write unanswered: exit 2
    # even though the closing re-read succeeded.
    assert code == 2
    assert output["sessionPoolFull"] is True
    assert output["waitedMs"] == 8400
    assert output["retryCount"] == 11
    if failure_phase == "confirmation":
        assert output["execution"]["steps"][0]["waitedMs"] == 8400
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_lan_acknowledgement_without_known_move_or_state_stays_unconfirmed(clock):
    from bgwcli.restore import RestoreStep

    client, wire = client_with(
        lambda request, number: html(form("dhcpserver", "192.0.2.254", "ipaddr") if number == 1 else SAVED_RED)
    )
    step = RestoreStep(1, "form", "dhcpserver", "LAN change", raw_payload={"Save": "Save"}, reconnect_required=True)
    result = execute_restore(client, [step]).steps[0]
    assert result.status == "failed" and result.error_type is None
    assert result.acknowledgement_observed is True and result.state_observed is None
    assert "reconnect" not in result.error
    assert [r.method for r in wire.requests] == ["GET", "POST", "GET", "GET", "GET"]


@pytest.mark.parametrize("missing", ["acknowledgement", "requested-state"])
@pytest.mark.parametrize("page", ["etherlan", "ippass", "wmacauth", "dhcpserver"])
def test_optional_restore_pages_keep_both_confirmation_requirements(clock, page, missing):
    body = form(
        page, "old" if missing == "requested-state" else "new", banner="" if missing == "acknowledgement" else SAVED_RED
    )
    client, wire = client_with(lambda request, n: html(form(page, "old") if n == 1 else body))
    result = execute_restore(client, plan(page)).steps[0]
    assert result.status == "failed"
    assert result.acknowledgement_observed is (missing != "acknowledgement")
    assert result.state_observed is (missing != "requested-state")
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("phase", ["initial", "rescan"])
def test_cli_preflight_coordination_preserves_pool_wait_metadata(monkeypatch, capsys, phase):
    import json

    from test_restore_preflight_integration import Fetcher, Router, baseline, pages

    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.config import GlobalOptions

    client = Router()
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", Fetcher(pages(reset=True)))

    def fail(*args, **kwargs):
        raise session_pool_full_error(waited_ms=3200, retry_count=5)

    if phase == "initial":
        monkeypatch.setattr(cli, "inspect_allocation_conflicts", fail)
    else:
        monkeypatch.setattr(
            cli,
            "inspect_allocation_conflicts",
            lambda *args, **kwargs: AllocationPreflight(
                [AllocationConflict("192.0.2.1", "02:00:00:00:00:01", "02:00:00:00:00:02", "holder", "off")],
                {"Clear": "Clear Device List"},
            ),
        )
        monkeypatch.setattr(cli, "rescan_allocation_conflicts", fail)
    command = cli.Command(
        "restore", ["synthetic.json"], GlobalOptions(json=True), commit=phase == "rescan", confirm="RESTORE"
    )
    coordination = cli._run_restore(client, command)
    output = json.loads(capsys.readouterr().out)
    assert coordination == {"sessionPoolFull": True, "waitedMs": 3200, "retryCount": 5}
    assert output["waitedMs"] == 3200 and output["retryCount"] == 5
    assert command.exit_code == 2
    assert not client.posts


@pytest.mark.parametrize("automatic", [False, True], ids=["restore", "autorestore"])
def test_applied_lan_address_change_retains_evidence_without_old_origin_snapshot(
    clock, tmp_env, tmp_path, capsys, monkeypatch, automatic
):
    import json
    from urllib.parse import urlsplit

    from bgwcli.autorestore import AutorestoreOptions, result_output, run_autorestore
    from bgwcli.dumpfile import write_dump_file
    from bgwcli.recovery_state import RecoveryCheckpoint

    selected = ("dosprotect", "wconfig", "dhcpserver")
    desired_html = {page: form(page, "same") for page in selected}
    desired_html["dhcpserver"] = form("dhcpserver", "192.0.2.1", "ipaddr", SAVED_RED)
    dump = extract_snapshot(
        {page: parse_page(page, body) for page, body in desired_html.items()},
        ts="",
        router_host="",
        include=("dhcpserver",),
    )
    posted = False

    def handle(request, number):
        nonlocal posted
        page = urlsplit(request.url).path.rsplit("/", 1)[-1].removesuffix(".ha")
        if request.method == "POST":
            assert page == "dhcpserver"
            posted = True
            return html(desired_html[page])
        if posted:
            raise TimeoutError("old LAN origin is gone")
        return html(form(page, "192.0.2.254", "ipaddr") if page == "dhcpserver" else desired_html[page])

    client, wire = client_with(handle)
    if automatic:
        checkpoint = RecoveryCheckpoint(client.session_identity(), dump, selected, root=tmp_path / "recovery")
        result = run_autorestore(
            lambda: (client, False),
            dump,
            AutorestoreOptions(commit=True, pages=selected, on_any_diff=True),
            fetch_pages=cli._fetch_snapshot_pages,
            checkpoint=checkpoint,
            log=lambda _: None,
        )
        output = result_output(result)
        step = output["passes"][0]["steps"][0]
        assert result.exit_code == 2
        assert output["passes"][0]["applied"] == 1
        assert output["passes"][0]["converged"] is None
        assert checkpoint.is_active()
        message = output["reason"]
    else:
        path = tmp_path / "lan.json"
        write_dump_file(path, dump)
        monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
        code = cli.main(
            ["restore", str(path), "--include", ",".join(selected), "--commit", "--confirm", "RESTORE", "--json"]
        )
        output = json.loads(capsys.readouterr().out)
        step = output["execution"]["steps"][0]
        assert code == 2
        message = output.get("verificationError", {}).get("message", "")
    assert [r.method for r in wire.requests] == ["GET", "GET", "GET", "GET", "POST"]
    assert output["diff"] is None
    assert step["status"] == "applied"
    assert step["acknowledgementObserved"] is True and step["stateObserved"] is True
    assert step["lanAddressChanged"] is True
    assert step["reconnectAddress"] == "192.0.2.1"
    assert "192.0.2.1" in message and "reconnect" in message.lower()
    assert [r.method for r in wire.requests] == ["GET", "GET", "GET", "GET", "POST"]
