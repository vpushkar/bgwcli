"""Offline regressions for honest final-write outcomes."""

from dataclasses import replace

import pytest
from test_save_confirmation import PENDING, SAVED_RED, MutationRouter, response
from test_save_confirmation import clock as confirmation_clock

from bgwcli.errors import RouterAuthError, RouterSessionPoolFullError
from bgwcli.parser import parse_page
from bgwcli.restore import RestoreOptions, build_restore_plan, execute_restore
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

clock = confirmation_clock


def form_html(page, value):
    return (
        f'<form action="/cgi-bin/{page}.ha"><input type="hidden" name="nonce" value="abc123">'
        f'<input type="text" name="'
        f'{"ipaddr" if page == "dhcpserver" else "key11"}" value="{value}">'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def form_steps(page="wconfig"):
    live = extract_snapshot(
        {page: parse_page(page, form_html(page, "old"), include_secrets=True)}, ts="", router_host=""
    )
    field = "ipaddr" if page == "dhcpserver" else "key11"
    dump = replace(live, forms={page: {field: "192.0.2.1" if page == "dhcpserver" else "secret-new"}})
    parsed = {page: parse_page(page, form_html(page, "old"), include_secrets=True)}
    return build_restore_plan(diff_snapshots(dump, live), dump, parsed, RestoreOptions(pages=(page,)))


def test_lan_save_requires_reconnect_without_old_address_polling(clock):
    router = MutationRouter([PENDING])
    result = execute_restore(router, form_steps("dhcpserver")).steps[0]
    assert result.status == "reconnect-required"
    assert result.write_attempted is True
    assert result.reconnect_address == "192.0.2.1"
    assert router.polls == 0
    assert len(router.posts) == 1


def test_transient_http_503_retries_read_only(clock):
    class Busy(MutationRouter):
        def get_cgi_page(self, page):
            if not self.polls:
                self.polls += 1
                return response(page, status=503)
            return super().get_cgi_page(page)

    router = Busy([SAVED_RED + form_html("wconfig", "secret-new")])
    result = execute_restore(router, form_steps()).steps[0]
    assert result.status == "applied"
    assert len(router.posts) == 1
    assert router.polls == 2


@pytest.mark.parametrize("error", [RouterAuthError, RouterSessionPoolFullError])
def test_terminal_auth_retains_sent_once_and_pool_signal(clock, error):
    class Expired(MutationRouter):
        def get_cgi_page(self, page):
            raise error("synthetic authentication failure")

    router = Expired([])
    result = execute_restore(router, form_steps()).steps[0]
    assert result.write_attempted is True
    assert "sent once" in result.error
    assert "verify" in result.error
    assert result.session_pool_full is (error is RouterSessionPoolFullError)
    assert len(router.posts) == 1


def test_stale_success_banner_cannot_confirm_dropped_form_write(clock):
    router = MutationRouter([SAVED_RED + form_html("wconfig", "old")])
    result = execute_restore(router, form_steps()).steps[0]
    assert result.status == "failed"
    assert result.acknowledgement_observed is True
    assert result.state_observed is False
    assert len(router.posts) == 1


def test_lost_ack_with_desired_state_remains_unconfirmed(clock):
    router = MutationRouter([form_html("wconfig", "secret-new")])
    result = execute_restore(router, form_steps()).steps[0]
    assert result.status == "failed"
    assert result.acknowledgement_observed is False
    assert result.state_observed is True
    assert len(router.posts) == 1


def test_unopened_allocation_is_an_explicit_nonwrite(clock):
    from test_restore import MAC, reserve_fake

    router = MutationRouter([PENDING])
    result = execute_restore(router, [reserve_fake(MAC, "192.0.2.3")]).steps[0]
    assert result.status == "blocked"
    assert result.write_attempted is False
    assert len(router.posts) == 1
    assert "Entry did not open" in result.error


def test_autorestore_retries_explicit_nonwrite_from_fresh_snapshot(monkeypatch):
    from test_autorestore import Fetcher, full_pages, harness, reset_pages

    import bgwcli.autorestore as auto
    from bgwcli.autorestore import AutorestoreOptions
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    fetcher = Fetcher(reset_pages(), reset_pages(), full_pages())
    calls = []

    def execute(client, steps, on_step):
        calls.append(len(fetcher.calls))
        if len(calls) == 1:
            return RestoreExecution(
                [
                    RestoreStepResult(
                        1, "ipalloc", "reserve", "reserve", "failed", error="entry did not open", write_attempted=False
                    )
                ],
                stopped_at=1,
            )
        return RestoreExecution([])

    monkeypatch.setattr(auto, "execute_restore", execute)
    result, _, sleeps, _ = harness(fetcher, AutorestoreOptions(commit=True, max_passes=2))
    assert result.status == "converged"
    assert calls == [1, 2]
    assert len(sleeps) == 1


def test_autorestore_pool_signal_survives_successful_closing_diff(monkeypatch):
    from test_autorestore import Fetcher, full_pages, harness, reset_pages

    import bgwcli.autorestore as auto
    from bgwcli.autorestore import AutorestoreOptions
    from bgwcli.restore import RestoreExecution, RestoreStepResult

    monkeypatch.setattr(
        auto,
        "execute_restore",
        lambda *args: RestoreExecution(
            [
                RestoreStepResult(
                    1,
                    "wconfig",
                    "form",
                    "save",
                    "failed",
                    error="sent once",
                    write_attempted=True,
                    session_pool_full=True,
                )
            ],
            stopped_at=1,
        ),
    )
    result, _, _, _ = harness(Fetcher(reset_pages(), full_pages()), AutorestoreOptions(commit=True))
    assert result.session_pool_full is True
    # The sent write met a full pool (no answer): exit 2 even though the closing diff was readable.
    assert result.status == "error" and result.write_unanswered is True


def test_cli_pool_full_after_save_preserves_exception_type(clock):
    from bgwcli.cli import _post_and_confirm

    class Expired(MutationRouter):
        def get_cgi_page(self, page):
            raise RouterSessionPoolFullError("pool exhausted")

    with pytest.raises(RouterSessionPoolFullError, match="sent once"):
        _post_and_confirm(Expired([]), "wconfig", {"Save": "Save"})


def test_cli_lan_set_reports_unavailable_verification_without_polling(monkeypatch, capsys, tmp_env):
    from test_cli import FakeClient, run_json

    from bgwcli import cli

    class Lan(FakeClient):
        def get_cgi_page(self, page, **kwargs):
            assert not self.posts, "must not poll the old address after a LAN Save"
            return response(page, form_html(page, "192.0.2.254"))

    monkeypatch.setenv("BGW_ACCESS_CODE", "synthetic")
    client = Lan()
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code, payload, _ = run_json(
        capsys, ["set", "dhcpserver", "ipaddr=192.0.2.1", "--commit", "--confirm", "DHCPSERVER", "--json"]
    )
    assert code == 2
    assert payload["outcome"] == "reconnect-required"
    assert payload["reconnectAddress"] == "192.0.2.1"
    assert "verified" not in payload


@pytest.mark.parametrize("page", ["services", "apphosting"])
def test_planned_table_add_requires_requested_row(clock, page):
    from integration_html import APPHOSTING_HTML, SERVICES_HTML, saved_configuration_html

    from bgwcli.snapshot import SnapshotForward, SnapshotService

    html = SERVICES_HTML if page == "services" else APPHOSTING_HTML
    parsed = {page: parse_page(page, html, include_secrets=True)}
    live = extract_snapshot(parsed, ts="", router_host="")
    if page == "services":
        wanted = replace(live, services=[SnapshotService("Fresh", 60001, 60010, 60001, "UDP")])
        current = replace(live, services=[])
    else:
        wanted = replace(live, forwards=[SnapshotForward("Mosh", "host-a", "aa:bb:cc:dd:ee:02")])
        current = replace(live, forwards=[])
    steps = build_restore_plan(diff_snapshots(wanted, current), wanted, parsed, RestoreOptions(pages=(page,)))
    # The acknowledgement page is readable (it shows the table) but lacks the requested row.
    mosh_row = '<tr><td>Mosh</td><td>host-a</td><td><input type="submit" name="Remove_2" value="Remove"></td></tr>'
    lacking = html.replace(mosh_row, "")
    router = MutationRouter([SAVED_RED + lacking])
    execution = execute_restore(router, steps)
    assert execution.steps[0].status == "failed"
    assert execution.steps[0].acknowledgement_observed is True
    assert execution.steps[0].state_observed is False
    router = MutationRouter([saved_configuration_html(page, steps[0].raw_payload)])
    assert execute_restore(router, steps).steps[0].status == "applied"


def test_real_allocation_nonwrite_retries_next_pass_and_saves_only_once(clock):
    from integration_html import allocation_saved_html, entry_page_html
    from page_builders import ipalloc_page

    from bgwcli.autorestore import AutorestoreOptions, run_autorestore
    from bgwcli.snapshot import SnapshotReservation

    mac, ip = "02:0a:0b:0c:0d:04", "192.0.2.3"
    initial = ipalloc_page(rows=[("192.0.2.4", mac, "on", "DHCP Allocation")])
    desired = replace(
        extract_snapshot({"ipalloc": initial}, ts="", router_host=""), reservations=[SnapshotReservation(mac, ip)]
    )

    class Router:
        def __init__(self):
            self.posts = []
            self.allocations = 0
            self.saved = False

        def post_cgi_page(self, page, fields):
            self.posts.append((page, dict(fields)))
            if "Save" in fields:
                self.saved = True
                return response(page, allocation_saved_html(mac, ip))
            self.allocations += 1
            return response(page, status=302)

        def get_cgi_page(self, page):
            if page == "devices":
                return response(
                    page,
                    "<title>Device List</title><table><tr><th>IPv4 Address / Name</th>"
                    "<th>MAC Address</th><th>Status</th></tr></table>",
                )
            if self.allocations >= 2:
                return response(page, entry_page_html(mac, [ip]))
            return response(
                page,
                "<title>IP Allocation</title><table><tr><th>IPv4 Address / Name</th>"
                "<th>MAC Address</th><th>Status</th><th>Allocation</th></tr></table>",
            )

    router = Router()
    fetches = []

    def fetch(client, pages):
        fetches.append(len(router.posts))
        return {"ipalloc": ipalloc_page(rows=[(ip, mac, "on", "Fixed Allocation")]) if router.saved else initial}, []

    result = run_autorestore(
        lambda: (router, False),
        desired,
        AutorestoreOptions(commit=True, pages=("ipalloc",), max_passes=2),
        fetch_pages=fetch,
        sleep=lambda _: None,
        log=lambda _: None,
    )
    assert result.status == "converged"
    assert fetches == [0, 1, 3]
    assert sum("Save" in fields for _, fields in router.posts) == 1
    assert result.passes[0]["steps"][0]["writeAttempted"] is False


def test_lan_disconnect_after_attempt_still_requires_reconnect(clock):
    from bgwcli.errors import RouterConnectionError

    class Moving(MutationRouter):
        def post_cgi_page(self, page, fields):
            self.posts.append((page, dict(fields)))
            raise RouterConnectionError("connection moved")

    router = Moving([])
    result = execute_restore(router, form_steps("dhcpserver")).steps[0]
    assert result.status == "reconnect-required"
    assert result.write_attempted is None and len(router.posts) == 1
    assert "unknown" in result.error and "sent once" not in result.error
    assert router.polls == 0


def test_restore_confirmation_metadata_cannot_expose_requested_password(clock):
    import json

    from bgwcli.format import display_restore_steps, execution_output

    steps = form_steps()
    router = MutationRouter([SAVED_RED + form_html("wconfig", "old")])
    result = execute_restore(router, steps)
    assert "secret-new" not in json.dumps(display_restore_steps(steps, False))
    assert "secret-new" not in json.dumps(execution_output(result))


@pytest.mark.parametrize("page", ["services", "apphosting"])
def test_remove_banner_with_unchanged_row_or_unrelated_table_is_not_confirmation(clock, page):
    from integration_html import APPHOSTING_HTML, SERVICES_HTML

    html = SERVICES_HTML if page == "services" else APPHOSTING_HTML
    parsed = {page: parse_page(page, html, include_secrets=True)}
    if page == "services":
        # A service remove is planned only once the forwards page shows no forward still using it.
        parsed["apphosting"] = parse_page("apphosting", "<html><body></body></html>", include_secrets=True)
    current = extract_snapshot(parsed, ts="", router_host="")
    wanted = replace(
        current,
        services=[] if page == "services" else current.services,
        forwards=[] if page == "apphosting" else current.forwards,
    )
    steps = build_restore_plan(
        diff_snapshots(wanted, current), wanted, parsed, RestoreOptions(pages=(page,), prune=True)
    )
    for body in [SAVED_RED + html, SAVED_RED + "<table><tr><td>Status</td><td>Online</td></tr></table>"]:
        router = MutationRouter([body])
        result = execute_restore(router, steps).steps[0]
        assert result.status == "failed"
        assert len(router.posts) == 1


def test_restore_text_keeps_verification_guidance_when_http_status_exists(clock):
    import io

    from bgwcli.format import print_restore_step_result

    result = execute_restore(MutationRouter([PENDING]), form_steps()).steps[0]
    stream = io.StringIO()
    print_restore_step_result(result, stream)
    assert "sent once" in stream.getvalue()
    assert "verify" in stream.getvalue()


def test_cli_lan_pool_failure_reaches_session_coordinator(monkeypatch, tmp_env, capsys):
    from test_cli import FakeClient, run_json

    from bgwcli import cli

    class PoolFull(FakeClient):
        def get_cgi_page(self, page, **kwargs):
            return response(page, form_html(page, "192.0.2.254"))

        def post_cgi_page(self, page, fields):
            raise RouterSessionPoolFullError("synthetic pool full")

    client = PoolFull()
    monkeypatch.setenv("BGW_ACCESS_CODE", "synthetic")
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code, payload, _ = run_json(
        capsys, ["set", "dhcpserver", "ipaddr=192.0.2.1", "--commit", "--confirm", "DHCPSERVER", "--json"]
    )
    assert code == 2
    assert payload["sessionPoolFull"] is True
    assert cli.read_session_state(client.origin).pool_cooldown_until is not None


def scripted_real_client(clock, verification_responses):
    """Inject only the wire transport; preserve nonce, HTTP-status, and auth handling."""
    from test_client import FakeTransport, html, make_client

    reads = []

    def handle(request, number):
        if number == 1:
            assert request.method == "GET"
            return html('<input name="nonce" value="abc123"><input type="submit" name="Save" value="Save">')
        if number == 2:
            assert request.method == "POST"
            return html("", status=302, headers={"location": "/cgi-bin/wconfig.ha"})
        assert request.method == "GET", "verification must never repeat the POST"
        reads.append(clock.now)
        return verification_responses[min(len(reads) - 1, len(verification_responses) - 1)]

    transport = FakeTransport(handle)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "synthetic"}})
    return client, transport, reads


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_real_client_transient_verification_http_errors_retry_only_reads(clock, status):
    from test_client import html

    client, transport, reads = scripted_real_client(
        clock, [html("busy", status=status), html(SAVED_RED + form_html("wconfig", "secret-new"))]
    )
    result = execute_restore(client, form_steps()).steps[0]
    assert result.status == "applied"
    assert result.state_observed is True and result.acknowledgement_observed is True
    assert [r.method for r in transport.requests] == ["GET", "POST", "GET", "GET"]
    assert reads == [0.0, 1.0]


def test_real_client_repeated_503_verification_stops_at_deadline(clock):
    from test_client import html

    client, transport, reads = scripted_real_client(clock, [html("busy", status=503)])
    result = execute_restore(client, form_steps()).steps[0]
    assert result.status == "failed" and result.write_attempted is True
    assert "Timed out" in result.error and "sent once" in result.error
    assert sum(r.method == "POST" for r in transport.requests) == 1
    assert reads == [0.0, 1.0, 2.0]
    assert clock.now == 3.0


@pytest.mark.parametrize("failure", ["auth", "pool", "not-found"])
def test_real_client_terminal_verification_errors_do_not_retry(clock, failure):
    from test_client import POOL_FULL_HTML, html

    failed_response = {
        "auth": html("forbidden", status=403),
        "pool": html(POOL_FULL_HTML),
        "not-found": html("not found", status=404),
    }[failure]
    client, transport, reads = scripted_real_client(
        clock, [failed_response, html(SAVED_RED + form_html("wconfig", "secret-new"))]
    )
    result = execute_restore(client, form_steps()).steps[0]
    assert result.status == "failed" and result.write_attempted is True
    assert "sent once" in result.error and "verify" in result.error
    assert result.session_pool_full is (failure == "pool")
    assert [r.method for r in transport.requests] == ["GET", "POST", "GET"]
    assert reads == [0.0]


@pytest.mark.parametrize("command_name", ["set", "submit"])
@pytest.mark.parametrize("failure", ["auth", "pool", "transport"])
def test_real_client_cli_final_post_failure_retains_type_and_attempt_guidance(tmp_env, capsys, command_name, failure):
    from test_client import POOL_FULL_HTML, FakeTransport, html, make_client

    from bgwcli import cli

    def handle(request, number):
        if request.method == "GET":
            assert number <= 2
            return html(form_html("wconfig", "old"))
        if failure == "transport":
            raise TimeoutError("synthetic lost final POST response")
        return html(POOL_FULL_HTML) if failure == "pool" else html("forbidden", status=403)

    transport = FakeTransport(handle)
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "synthetic"}})
    argv = [
        command_name,
        "wconfig",
        *(["Save"] if command_name == "submit" else []),
        "key11=secret-new",
        "--commit",
        "--confirm",
        "WCONFIG",
    ]
    if failure == "transport":
        # A lost final POST is a structured failed result (exit 2) like on every other page; the
        # connection error type is kept in the step evidence, not raised.
        assert cli.run(cli.parse_args(argv), client=client) == 2
        out = capsys.readouterr().out
        assert f"{command_name}: failed" in out and "attempted once" in out
        assert "verify the gateway state before retrying" in out
        assert [r.method for r in transport.requests] == ["GET", "GET", "POST"]
        return
    if failure == "auth":
        # A 403 answering the write POST is that page's own answer (page-level), not a lost session:
        # a structured failed result (exit 2) that keeps the "sent once" evidence, not a raise.
        assert cli.run(cli.parse_args(argv), client=client) == 2
        out = capsys.readouterr().out
        assert f"{command_name}: failed" in out and "HTTP 403" in out and "sent once" in out
        assert "verify the gateway state before retrying" in out
        assert [r.method for r in transport.requests] == ["GET", "GET", "POST"]
        return
    error_type = {"pool": RouterSessionPoolFullError}[failure]
    with pytest.raises(error_type) as caught:
        cli.run(cli.parse_args(argv), client=client)
    assert "sent once" in str(caught.value)
    assert "verify the gateway state before retrying" in str(caught.value)
    assert [r.method for r in transport.requests] == ["GET", "GET", "POST"]
    if failure == "pool":
        assert cli.read_session_state(client.session_identity()).pool_cooldown_until is not None


def test_cli_pool_json_keeps_final_write_guidance(monkeypatch, tmp_env, capsys):
    import json

    from test_client import POOL_FULL_HTML, FakeTransport, html, make_client

    from bgwcli import cli

    def handle(request, number):
        if request.method == "GET":
            return html(form_html("wconfig", "old"))
        return html(POOL_FULL_HTML)

    client = make_client(FakeTransport(handle))
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "synthetic"}})
    monkeypatch.setenv("BGW_ACCESS_CODE", "synthetic")
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "wconfig", "key11=secret-new", "--commit", "--confirm", "WCONFIG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 2 and output["sessionPoolFull"] is True
    assert "sent once" in output["error"]
    assert "verify the gateway state before retrying" in output["error"]
    assert "secret-new" not in output["error"]


def test_a_service_name_already_on_the_router_is_never_added_a_second_time():
    from bgwcli.snapshot import SnapshotService

    html = (
        '<form><input name="nonce" value="n"><table>'
        "<tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th></tr>"
        "<tr><td>Probe</td><td>80-80</td><td>80</td><td>TCP</td></tr></table></form>"
    )
    parsed = {"services": parse_page("services", html, include_secrets=True)}
    live = extract_snapshot(parsed, ts="", router_host="")
    wanted = replace(live, services=[
        SnapshotService("Probe", 80, 80, 80, "TCP"), SnapshotService("Probe", 81, 81, 81, "TCP"),
    ])
    steps = build_restore_plan(diff_snapshots(wanted, live), wanted, parsed, RestoreOptions(pages=("services",)))
    adds = [s for s in steps if s.kind == "add-service"]
    assert adds and all(s.blocked is not None and s.raw_payload is None or s.blocked for s in adds)
    assert all("already on the router" in (s.blocked or "") for s in adds)
