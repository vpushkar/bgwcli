"""Committed actions and diagnostics whose write POST fails report a structured result with exit 2.

Drives `cli.main([... "--json"])` over the real client with an in-memory transport, so the
transport's write counters decide the write evidence.
"""

from __future__ import annotations

import json

import pytest
from save_helpers import FakeTransport, client_with, html

from bgwcli import cli, session
from bgwcli.client import BGW320Client

SCAN_FORM = (
    '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old">'
    '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
)
CONGESTION_FORM = (
    '<form action="/cgi-bin/lanstatistics.ha"><input name="nonce" value="abc123">'
    '<input type="submit" name="Congestion" value="Congestion Detection 2.4 GHz"></form>'
)
DIAG_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value="">'
    '<input type="submit" name="Ping" value="Ping"></form>'
)
# A nonce page whose markup has no form tag: the page-level nonce serves whichever CGI is posted to
# (a form posting to another CGI is refused, so a form-less page keeps these write-path tests generic).
NONCE_PAGE = '<input name="nonce" value="abc123"><input type="submit" name="Go" value="Go">'

# argv -> page the nonce/plan reads come from
CASES = {
    "form_button": (["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"], SCAN_FORM),
    # Actions whose answer page is read (the Wi-Fi radio actions drop it, see test_action_answer_read).
    "read_form_button": (
        ["action", "detect-wifi-congestion-2.4", "--commit", "--confirm", "CONGESTION-2.4"], CONGESTION_FORM
    ),
    "post_path": (["action", "restart-wifi-2.4", "--commit", "--confirm", "RESTART-WIFI"], NONCE_PAGE),
    "plain": (["action", "run-speed-test", "--commit", "--confirm", "SPEED"], NONCE_PAGE),
    "diagnostic": (["diagnostics", "ping", "example.com", "--commit", "--confirm", "DIAG"], DIAG_FORM),
}


def _run(monkeypatch, capsys, argv, handle):
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("kind", list(CASES))
def test_http_500_answering_the_write_is_a_structured_failure_with_exit_2(clock, tmp_env, capsys, monkeypatch, kind):
    argv, page = CASES[kind]

    def handle(request, n):
        if request.method == "POST":
            return html("<html>Internal Server Error</html>", status=500)
        return html(page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed" and output["dryRun"] is False
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 500
    assert "HTTP 500" in output["warning"] and "verify the gateway state" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("kind", list(CASES))
def test_connection_error_on_the_write_is_a_structured_failure_with_exit_2(clock, tmp_env, capsys, monkeypatch, kind):
    argv, page = CASES[kind]

    def handle(request, n):
        if request.method == "POST":
            raise TimeoutError("router unreachable while posting")
        return html(page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is False
    assert "statusCode" not in output
    assert "Timed out connecting" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("kind", ["read_form_button", "plain"])
def test_successful_actions_are_unchanged(clock, tmp_env, capsys, monkeypatch, kind):
    argv, page = CASES[kind]

    def handle(request, n):
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(page)

    code, output, _ = _run(monkeypatch, capsys, argv, handle)

    assert code == 0
    assert output["committed"] is True and output["statusCode"] == 302 and output["location"] == "/cgi-bin/home.ha"
    assert "outcome" not in output and "writeAttempted" not in output


SUBMIT_PAGE = (
    '<form action="/cgi-bin/etherlan.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old"><input type="submit" name="Refresh" value="Refresh"></form>'
)
LOGIN_REDIRECT_CASES = {
    **{kind: CASES[kind] for kind in ("form_button", "post_path", "plain")},
    "submit": (["submit", "etherlan", "Refresh", "--commit", "--confirm", "ETHERLAN"], SUBMIT_PAGE),
}


@pytest.mark.parametrize("kind", list(LOGIN_REDIRECT_CASES))
def test_write_redirected_to_the_login_page_is_a_lost_session_not_a_commit(clock, tmp_env, capsys, monkeypatch, kind):
    """The gateway bounced the POST to login.ha: the session is gone and the write was not accepted.
    No answer (exit 2), never `committed: true`, and the POST is not sent a second time."""
    argv, page = LOGIN_REDIRECT_CASES[kind]

    def handle(request, n):
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(page)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()

    assert code == 2
    assert '"committed": true' not in captured.out
    assert "login page" in captured.err
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_write_redirected_to_the_login_page_forgets_the_cached_session(clock, tmp_env, capsys, monkeypatch):
    origin = "http://router.local"
    paths = session.session_paths(origin)
    session._ensure_private_dir(paths.cache.parent)
    now = session._now_ms()
    session._write_json(paths.cache, {
        "origin": origin, "authenticated": True, "cookies": {"sid": "cached"}, "version": 1,
        "cachedAt": now, "expiresAt": now + 600_000,
    })

    def handle(request, n):
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(NONCE_PAGE)

    transport = FakeTransport(handle)
    client = BGW320Client(origin, access_code="12345", timeout_ms=1000, user_agent="test", transport=transport)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["action", "run-speed-test", "--commit", "--confirm", "SPEED", "--host", origin, "--json"])
    capsys.readouterr()

    assert code == 2
    assert not paths.cache.exists(), "the gateway dropped the session; the cached copy is dead"
    assert sum(r.method == "POST" for r in transport.requests) == 1


def _scan_flow(after_post, form=SCAN_FORM):
    """find-best-channel-5: the scan POST answers 302 -> `after_post[0]`; GETs after the POST are
    answered by `after_post[1](request)`."""
    location, answer = after_post
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": location})
        if state["posted"]:
            return answer(request)
        return html(form)

    return handle


def test_unconfirmed_form_button_action_is_a_structured_failure_with_exit_2(clock, tmp_env, capsys, monkeypatch):
    """The Wi-Fi Warning page the scan redirects to answers a page-level 403: Continue was never posted,
    the action is unconfirmed (exit 2) and JSON mode still gets the result with its evidence."""
    argv, _ = CASES["form_button"]
    handle = _scan_flow(("/cgi-bin/wifiwarn_advanced.ha", lambda r: html("<html>Forbidden</html>", status=403)))

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    # The scan POST was sent and answered (the redirect); only Continue was never posted.
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 302 and output["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "HTTP 403" in output["warning"] and "Continue was not posted" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_rejected_form_button_action_is_a_structured_negative_answer(clock, tmp_env, capsys, monkeypatch):
    """The gateway answered the write with an error banner: a definitive negative (exit 1) that JSON
    mode reports as a structured `failed` result, not as stderr only."""
    argv, _ = CASES["read_form_button"]
    banner = (
        '<img id="error-message-icon" src="/images/icon_error.png">'
        '<div id="error-message-text">A required setting is empty</div>'
    )

    code, output, wire = _run(
        monkeypatch, capsys, argv,
        _scan_flow(("/cgi-bin/lanstatistics.ha", lambda r: html(banner + CONGESTION_FORM)), CONGESTION_FORM),
    )

    assert code == 1
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["warning"].startswith("router rejected the change: A required setting is empty")
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 302 and output["location"] == "/cgi-bin/lanstatistics.ha"
    assert sum(r.method == "POST" for r in wire.requests) == 1


POLL_FAULTS = {
    "http500": lambda request: html("<html>Internal Server Error</html>", status=500),
    "connection-reset": lambda request: (_ for _ in ()).throw(ConnectionResetError("connection reset by peer")),
    "page-403": lambda request: html("<html>Forbidden</html>", status=403),
}


@pytest.mark.parametrize("fault", list(POLL_FAULTS))
def test_diagnostic_result_poll_failure_reports_the_committed_run_with_exit_2(
    clock, tmp_env, capsys, monkeypatch, fault
):
    """The diagnostic POST was answered (302) so the run started; the result poll then fails. The
    run is reported as committed with the POST's evidence and the poll failure, exit 2."""
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda seconds: None)
    argv, _ = CASES["diagnostic"]
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/diag.ha"})
        if state["posted"] and n > 4:  # the first poll read shows no output yet; the next one fails
            return POLL_FAULTS[fault](request)
        return html(DIAG_FORM)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert output["committed"] is True and output["dryRun"] is False
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 302
    assert "could not read the diagnostic result" in output["warning"]
    assert ".." not in output["warning"], output["warning"]  # guidance appended as one sentence
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_diagnostic_result_poll_losing_the_session_still_raises(clock, tmp_env, capsys, monkeypatch):
    """A session-wide authentication failure during the poll keeps raising (exit 2) like everywhere."""
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda seconds: None)
    argv, _ = CASES["diagnostic"]
    login = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/diag.ha"})
        if state["posted"]:
            return html(login, status=403)
        return html(DIAG_FORM)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()

    assert code == 2
    assert '"committed": true' not in captured.out
    assert "HTTP 403" in captured.err
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("kind", ["post_path", "plain"])
def test_action_redirected_to_login_reports_the_sent_write_in_the_json_error(
    clock, tmp_env, capsys, monkeypatch, kind
):
    argv, page = CASES[kind]

    def handle(request, n):
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        return html(page)

    code, error, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert error["ok"] is False and error["errorType"] == "RouterAuthError"
    assert error["writeAttempted"] is True and "sent once" in error["error"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


POOL_FULL_PAGE = "<title>Login</title><p>All web server sessions are in use</p>"


@pytest.mark.parametrize("fault", ["session-lost", "pool-full"])
def test_diagnostic_poll_raising_reports_the_sent_diagnostic_in_the_json_error(
    clock, tmp_env, capsys, monkeypatch, fault
):
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda seconds: None)
    argv, _ = CASES["diagnostic"]
    login = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/diag.ha"})
        if state["posted"]:
            return html(login, status=403) if fault == "session-lost" else html(POOL_FULL_PAGE)
        return html(DIAG_FORM)

    code, error, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2 and error["ok"] is False
    assert error.get("sessionPoolFull") is (True if fault == "pool-full" else None)
    assert error["writeAttempted"] is True and "sent once" in error["error"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


# --- a rejection banner answering the diagnostic POST is a negative answer, not a run to poll --------------

from save_helpers import ERROR  # noqa: E402 - grouped with the tests that use it


@pytest.mark.parametrize("answer", ["200-inline", "302-redirect"])
def test_diagnostic_post_answered_by_a_rejection_banner_is_a_structured_rejection(
    clock, tmp_env, capsys, monkeypatch, answer
):
    from bgwcli import diagnostics

    monkeypatch.setattr(diagnostics, "_sleep", lambda seconds: None)
    argv, _ = CASES["diagnostic"]
    state = {"posted": False}
    rejected_page = ERROR + DIAG_FORM

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            if answer == "200-inline":
                return html(rejected_page, status=200)
            return html("", status=302, headers={"location": "/cgi-bin/diag.ha"})
        if state["posted"]:
            return html(rejected_page)
        return html(DIAG_FORM)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 1
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["warning"].startswith("router rejected the change: A required setting is empty")
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == (200 if answer == "200-inline" else 302)
    reads_after_post = wire.requests[[r.method for r in wire.requests].index("POST") + 1:]
    # 200 inline: nothing more is read; 302: exactly the one redirect-target read, no result polling.
    assert len(reads_after_post) == (0 if answer == "200-inline" else 1)
    assert sum(r.method == "POST" for r in wire.requests) == 1


# --- a rejection banner answering a plain or post_path action is a negative answer too -------------------


@pytest.mark.parametrize("answer", ["200-inline", "302-redirect"])
@pytest.mark.parametrize("kind", ["read_form_button", "plain"])
def test_action_post_answered_by_a_rejection_banner_is_a_structured_rejection(
    clock, tmp_env, capsys, monkeypatch, kind, answer
):
    argv, page = CASES[kind]
    state = {"posted": False}
    rejected_page = ERROR + page

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            if answer == "200-inline":
                return html(rejected_page, status=200)
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        if state["posted"]:
            return html(rejected_page)
        return html(page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 1
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["warning"].startswith("router rejected the change: A required setting is empty")
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == (200 if answer == "200-inline" else 302)
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("kind", ["read_form_button", "plain"])
def test_action_redirect_target_unreadable_is_no_answer(clock, tmp_env, capsys, monkeypatch, kind):
    """An action whose effect leaves the gateway up but whose redirect target cannot be read has no
    answer: the write was sent once, so exit 2 and not committed."""
    argv, page = CASES[kind]
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        if state["posted"]:
            raise ConnectionResetError("gateway restarting")
        return html(page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2
    assert output["committed"] is False and output["statusCode"] == 302 and output["writeAttempted"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


from save_helpers import NO_CHANGE  # noqa: E402 - grouped with the tests that use it


@pytest.mark.parametrize("answer", ["200-inline", "302-redirect"])
@pytest.mark.parametrize("kind", ["read_form_button", "plain"])
def test_action_answered_by_no_changes_detected_is_unchanged_with_exit_0(
    clock, tmp_env, capsys, monkeypatch, kind, answer
):
    """An action requests no field values, so "No changes detected. Save not performed." is a
    complete answer: nothing was saved and nothing needed saving (unchanged, exit 0)."""
    argv, page = CASES[kind]
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            if answer == "200-inline":
                return html(NO_CHANGE + page, status=200)
            return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
        return html(NO_CHANGE + page if state["posted"] else page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 0
    assert output["committed"] is False and output["outcome"] == "unchanged"
    assert output["writeAttempted"] is True and output["writePerformed"] is False
    assert "No changes detected" in output["result"]
    assert sum(r.method == "POST" for r in wire.requests) == 1
