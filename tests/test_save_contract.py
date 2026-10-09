"""One save-outcome contract for every `set`/`submit` path: Wi-Fi, LAN and generic pages.

| Gateway outcome                                   | Exit | Outcome     |
| "Changes saved" + post-save verify passes         | 0    | applied     |
| "No changes detected" + live form equals request  | 0    | unchanged   |
| "No changes detected" + live form differs         | 1    | failed      |
| Router error banner                               | 1    | failed      |
| No acknowledgement within the timeout             | 2    | failed      |

Each test drives the production entry point (`cli.main`), so the dispatch into `_report_wifi_save`,
`_report_lan_save` or the generic `_post_and_confirm` path is exercised, not a helper in isolation.
The "No changes detected" notification is recognised on every form page (the live gateway shows it
on the Wi-Fi pages; the live re-read decides the outcome wherever it appears).
"""

from __future__ import annotations

import json

import pytest
from save_helpers import ERROR, NO_CHANGE, SAVED_RED, client_with, form, html

from bgwcli import cli, restore

# page -> (field, old value, new value, confirmation token)
PAGES = {
    "wconfig": ("setting", "old", "new", "WCONFIG"),
    "dhcpserver": ("ipmask", "255.255.255.0", "255.255.0.0", "DHCPSERVER"),
    "dosprotect": ("setting", "old", "new", "DOSPROTECT"),
}
PATH_IDS = ["wifi", "lan", "generic"]

EXPECTED = {
    # outcome -> (exit code, outcome string, committed, writePerformed)
    "applied": (0, "applied", True, True),
    "no-changes-equal": (0, "unchanged", False, False),
    "no-changes-differ": (1, "failed", False, False),
    "rejected": (1, "failed", False, None),
    "unconfirmed": (2, "failed", False, None),
}


def _run(monkeypatch, capsys, page, outcome, operation="set"):
    field, old, new, token = PAGES[page]
    live_after = old if outcome in {"no-changes-differ", "unconfirmed"} else new
    banner = {
        "applied": SAVED_RED,
        "no-changes-equal": NO_CHANGE,
        "no-changes-differ": NO_CHANGE,
        "rejected": ERROR,
        "unconfirmed": "",
    }[outcome]
    client, wire = client_with(lambda request, n: html(
        form(page, old, field) if n <= 2 else form(page, live_after, field, banner)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    args = [operation, page, *(["Save"] if operation == "submit" else []), f"{field}={new}"]
    code = cli.main([*args, "--commit", "--confirm", token, "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("page", list(PAGES), ids=PATH_IDS)
def test_no_changes_with_matching_live_form_is_verified_and_exits_0(
    clock, tmp_env, capsys, monkeypatch, page
):
    code, output, wire = _run(monkeypatch, capsys, page, "no-changes-equal")

    assert code == 0
    assert output["outcome"] == "unchanged" and output["committed"] is False
    assert output["writePerformed"] is False and output["writeAttempted"] is True
    # The decision rests on a live re-read, not on the notification alone.
    assert output["verified"] is True
    assert "matches the requested values" in output["result"]
    assert [r.method for r in wire.requests][-1] == "GET", "the live form must be re-read before deciding"
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("page", list(PAGES), ids=PATH_IDS)
def test_no_changes_with_differing_live_form_is_a_negative_answer(
    clock, tmp_env, capsys, monkeypatch, page
):
    field, old, new, _ = PAGES[page]
    code, output, wire = _run(monkeypatch, capsys, page, "no-changes-differ")

    assert code == 1
    assert output["outcome"] == "failed" and output["committed"] is False
    assert output["writePerformed"] is False
    assert output["verified"] is False
    assert output["mismatches"] == {field: {"wanted": new, "live": old}}
    assert "ignored or normalised" in output["warning"] and field in output["warning"]
    assert "result" not in output, "the no-change sentence belongs to warning only on a negative answer"
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("outcome", list(EXPECTED))
@pytest.mark.parametrize("page", list(PAGES), ids=PATH_IDS)
def test_same_gateway_outcome_yields_the_same_exit_on_every_save_path(
    clock, tmp_env, capsys, monkeypatch, page, outcome
):
    exit_code, outcome_name, committed, performed = EXPECTED[outcome]
    code, output, wire = _run(monkeypatch, capsys, page, outcome)

    assert code == exit_code
    assert output["outcome"] == outcome_name
    assert output["committed"] is committed
    assert output.get("writePerformed") is performed
    assert output["writeAttempted"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1, "a write is never resent"
    if outcome == "rejected":
        assert "router rejected the change: A required setting is empty" in output["warning"]
    if outcome == "unconfirmed":
        assert "Timed out" in output["warning"]


@pytest.mark.parametrize("page", list(PAGES), ids=PATH_IDS)
def test_submit_with_requested_values_follows_the_same_contract(
    clock, tmp_env, capsys, monkeypatch, page
):
    code, output, _ = _run(monkeypatch, capsys, page, "no-changes-differ", operation="submit")
    assert code == 1 and output["outcome"] == "failed" and output["verified"] is False
    code, output, _ = _run(monkeypatch, capsys, page, "no-changes-equal", operation="submit")
    assert code == 0 and output["outcome"] == "unchanged" and output["verified"] is True


def test_lan_address_move_is_applied_without_re_reading_the_old_address(clock, tmp_env, capsys, monkeypatch):
    """After an applied LAN address move the old endpoint must not be read again: the reconnect is
    the verification, so the result is applied with the reconnect address and `verified` unset."""
    reads_after_post = []

    def handle(request, n):
        if n <= 2:
            return html(form("dhcpserver", "192.0.2.254", "ipaddr"))
        if request.method == "GET":
            reads_after_post.append(request.url)
            raise AssertionError("old LAN address read after the move")
        return html(form("dhcpserver", "192.0.2.1", "ipaddr", SAVED_RED))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "dhcpserver", "ipaddr=192.0.2.1", "--commit", "--confirm", "DHCPSERVER", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert reads_after_post == []
    assert code == 0 and output["committed"] is True and output["outcome"] == "applied"
    assert output["reconnectAddress"] == "192.0.2.1"
    assert output.get("verified") is None and output.get("mismatches") is None
    assert output.get("warning") is None
    assert sum(r.method == "POST" for r in wire.requests) == 1


# page -> (field, old, new, token) for the HTTP-error rows; etherlan is a plain generic form page.
HTTP_ERROR_PAGES = {
    "wconfig": PAGES["wconfig"],
    "dhcpserver": PAGES["dhcpserver"],
    "etherlan": ("setting", "old", "new", "ETHERLAN"),
}


def _run_set(monkeypatch, capsys, page, handle):
    field, old, new, token = HTTP_ERROR_PAGES[page]
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", page, f"{field}={new}", "--commit", "--confirm", token, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("page", list(HTTP_ERROR_PAGES))
def test_http_error_from_the_save_post_is_unconfirmed_json_on_every_path(clock, tmp_env, capsys, monkeypatch, page):
    """A 5xx answer to the write itself is "could not answer" (exit 2) with the step evidence, on the
    generic path exactly as on the Wi-Fi and LAN paths; it is not a plain exit 1 error line."""
    field, old, _, _ = HTTP_ERROR_PAGES[page]

    def handle(request, n):
        if request.method == "POST":
            return html("<html>Internal Server Error</html>", status=500)
        return html(form(page, old, field))

    code, output, wire = _run_set(monkeypatch, capsys, page, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True
    assert output.get("writePerformed") is not True and output.get("acknowledgementObserved") is not True
    assert "HTTP 500" in output["warning"]
    assert ".." not in output["warning"], output["warning"]  # guidance is appended as one sentence
    assert sum(r.method == "POST" for r in wire.requests) == 1
    if page == "etherlan":
        # The generic step states the missing acknowledgement explicitly (restore's execution-error
        # step for Wi-Fi/LAN leaves the field unset; both are "not observed").
        assert output["acknowledgementObserved"] is False


def test_session_wide_auth_error_during_the_re_read_keeps_the_acknowledgement(clock, tmp_env, capsys, monkeypatch):
    """`Changes saved` was observed; a lost session on the verification re-read makes the re-read
    unavailable (verified unset, warning names the cause) but must not discard the evidence or the
    JSON. Its exit matches the other "re-read unavailable" flavour (HTTP 500 on the re-read)."""
    field, old, new, _ = HTTP_ERROR_PAGES["etherlan"]
    posted = {"done": False}

    def make_handle(reread):
        posted["done"] = False

        def handle(request, n):
            if request.method == "POST":
                posted["done"] = True
                return html(form("etherlan", new, field, SAVED_RED))
            if posted["done"]:
                return reread()
            return html(form("etherlan", old, field))
        return handle

    auth_lost = make_handle(lambda: html("<html><head><title>Login</title></head></html>", status=403))
    code_auth, output_auth, _ = _run_set(monkeypatch, capsys, "etherlan", auth_lost)
    assert output_auth["committed"] is True and output_auth["outcome"] == "applied"
    assert output_auth["acknowledgementObserved"] is True and output_auth["writePerformed"] is True
    assert output_auth.get("verified") is None and output_auth.get("mismatches") is None
    assert "HTTP 403" in output_auth["warning"] and "sent once" in output_auth["warning"]

    server_error = make_handle(lambda: html("<html>Internal Server Error</html>", status=500))
    code_500, output_500, _ = _run_set(monkeypatch, capsys, "etherlan", server_error)
    assert output_500["committed"] is True and output_500.get("verified") is None
    # An acknowledged save whose re-read could not complete is "could not answer": exit 2 for both
    # flavours, with committed still true so scripts know not to resend.
    assert code_auth == 2 and code_500 == 2


# --- verification re-read retries ---------------------------------------------------------------

RETRY_PAGES = {
    "wconfig": PAGES["wconfig"],
    "dhcpserver": PAGES["dhcpserver"],
    "etherlan": ("setting", "old", "new", "ETHERLAN"),
}
LOGIN_PAGE = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'


def _scripted(page, field, live, banner, rereads):
    """Serve the plan/nonce reads, the POST answer, then one scripted answer per re-read of `page`:
    "500" (HTTP 500), "lost" (403 + Login page: session gone), "page-403" (403, the page's own answer),
    "drop" (connection loss), "429"/"408" (HTTP 429/408), "lost-bad-login" (session gone and the
    next login is refused with HTTP 401), "ok" (the live form). Login requests are answered with a
    nonce and a 302 to home.ha. Unscripted re-reads fail the test."""
    script = list(rereads)
    state = {"posted": False, "login_refused": False, "login_failed": False}

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        if path.startswith("login.ha"):
            if state["login_refused"]:
                return html("<html>Unauthorized</html>", status=401)
            if request.method == "POST" and state["login_failed"]:
                return html("<html><body>Login Failed</body></html>")
            if request.method == "POST":
                return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
            return html(LOGIN_PAGE)
        if request.method == "POST":
            state["posted"] = True
            return html(form(page, live, field, banner))
        if not state["posted"]:
            return html(form(page, "old" if page != "dhcpserver" else "255.255.255.0", field))
        assert script, f"unexpected re-read #{len(rereads) + 1} of {page}"
        answer = script.pop(0)
        if answer == "500":
            return html("<html>Internal Server Error</html>", status=500)
        if answer == "lost":
            return html(LOGIN_PAGE, status=403)
        if answer == "lost-bad-login":
            state["login_refused"] = True
            return html(LOGIN_PAGE, status=403)
        if answer == "lost-login-failed":
            # 403 + Login page: the client does not log in itself; the re-read's own login is refused.
            state["login_failed"] = True
            return html(LOGIN_PAGE, status=403)
        if answer == "bounce-login-failed":
            # 302 -> login.ha -> 200 Login page: the client logs in itself and the gateway refuses it.
            state["login_failed"] = True
            return html("", status=302, headers={"location": "/cgi-bin/login.ha"})
        if answer in ("429", "408", "501"):
            return html("<html>Busy</html>", status=int(answer))
        if answer == "page-403":
            return html("<html>Forbidden</html>", status=403)
        if answer == "drop":
            raise TimeoutError("connection dropped")
        return html(form(page, live, field))

    return handle


def _run_retry(monkeypatch, capsys, page, banner, rereads, *, live=None):
    field, old, new, token = RETRY_PAGES[page]
    client, wire = client_with(_scripted(page, field, live or new, banner, rereads))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", page, f"{field}={new}", "--commit", "--confirm", token, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    page_reads = [r for r in wire.requests if r.method == "GET" and r.url.endswith(f"/{page}.ha")]
    return code, json.loads(captured.out), wire, page_reads


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_re_read_retries_once_after_a_500_and_verifies(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page):
    code, output, _, reads = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["500", "ok"])
    assert code == 0 and output["committed"] is True and output["verified"] is True
    assert output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]
    assert len(reads) == 2 + 2  # plan fetch + nonce read, then two verification attempts


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_re_read_retries_twice_with_incremental_delay(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page):
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["500", "500", "ok"])
    assert code == 0 and output["verified"] is True
    assert output["verifyAttempts"] == 3
    assert verify_sleeps == [2.0, 4.0]


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_exhausted_re_read_keeps_the_acknowledged_save_unverified(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    code, output, _, reads = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["500", "500", "500"])
    assert code == 2 and output["committed"] is True and output["outcome"] == "applied"
    assert output.get("verified") is None
    assert output["verifyAttempts"] == 3
    assert "3 attempts" in output["warning"] and "HTTP 500" in output["warning"]
    assert ".." not in output["warning"], output["warning"]
    assert verify_sleeps == [2.0, 4.0]
    assert len(reads) == 2 + 3, "no fourth verification read"


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_lost_session_on_re_read_logs_in_again_before_retrying(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    code, output, wire, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["lost", "ok"])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]
    after_post = wire.requests[[r.method for r in wire.requests].index("POST") + 1:]
    kinds = [f"{r.method} {r.url.rsplit('/', 1)[-1]}" for r in after_post]
    first, last = kinds.index(f"GET {page}.ha"), len(kinds) - 1 - kinds[::-1].index(f"GET {page}.ha")
    assert "POST login.ha" in kinds[first:last], kinds


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_session_lost_again_after_the_re_reads_own_login_is_retried(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    """Only a login the client performs during the GET makes a lost session final; the re-read's own
    forced login before the GET does not."""
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["lost", "lost", "ok"])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 3
    assert verify_sleeps == [2.0, 4.0]


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_refused_login_during_the_re_read_ends_the_retries(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page):
    code, output, wire, reads = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["lost-bad-login", "ok", "ok"])
    assert code == 2 and output["committed"] is True and output.get("verified") is None
    assert output["verifyAttempts"] == 2
    assert "HTTP 401" in output["warning"] and "2 attempts" in output["warning"]
    assert verify_sleeps == [2.0], "no further sleep after a refused login"
    assert len(reads) == 2 + 1, "the page is not read again after a refused login"


@pytest.mark.parametrize(("shape", "attempts", "sleeps"), [
    ("bounce-login-failed", 1, []),  # the client already tried (and failed) to log in again
    ("lost-login-failed", 2, [2.0]),  # the re-read's own forced login is refused
])
@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_refused_re_login_ends_the_retries_on_every_bounce_shape(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page, shape, attempts, sleeps
):
    code, output, wire, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, [shape, "ok", "ok"])
    assert code == 2 and output["committed"] is True and output.get("verified") is None
    assert output["verifyAttempts"] == attempts
    assert "Login failed" in output["warning"]
    assert verify_sleeps == sleeps
    login_posts = [r for r in wire.requests if r.method == "POST" and r.url.endswith("/login.ha")]
    assert len(login_posts) == 1, "the refused login is never repeated"


@pytest.mark.parametrize("status", ["429", "408"])
@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_re_read_retries_408_and_429(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page, status):
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, [status, "ok"])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]


def test_re_read_relogin_is_forced(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    field, _, new, token = RETRY_PAGES["etherlan"]
    client, _ = client_with(_scripted("etherlan", field, new, SAVED_RED, ["lost", "ok"]))
    calls = []
    original = client.login

    def login(*args, **kwargs):
        calls.append(kwargs.get("force", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "login", login)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "etherlan", f"{field}={new}", "--commit", "--confirm", token, "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["verifyAttempts"] == 2
    assert calls and calls[-1] is True, calls


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_connection_loss_on_re_read_is_retried(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page):
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["drop", "ok"])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_no_changes_re_read_is_retried_before_judging(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, NO_CHANGE, ["500", "ok"])
    assert code == 0 and output["outcome"] == "unchanged" and output["verified"] is True
    assert output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_no_changes_with_exhausted_re_read_is_still_unknown(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    code, output, _, _ = _run_retry(monkeypatch, capsys, page, NO_CHANGE, ["500", "500", "500"])
    assert code == 2 and output["outcome"] == "failed" and output["committed"] is False
    assert output.get("verified") is None and output["verifyAttempts"] == 3
    assert "3 attempts" in output["warning"] and "No changes detected" in output["warning"]
    assert "result" not in output
    assert verify_sleeps == [2.0, 4.0]


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_page_level_403_on_re_read_is_not_retried(clock, tmp_env, capsys, monkeypatch, verify_sleeps, page):
    code, output, _, reads = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["page-403"])
    # Attempted but unavailable re-read (not retried): still "could not answer", exit 2.
    assert code == 2 and output["committed"] is True and output.get("verified") is None
    assert output["verifyAttempts"] == 1
    assert "HTTP 403" in output["warning"]
    assert verify_sleeps == []
    assert len(reads) == 2 + 1


def test_first_try_verification_reports_a_single_attempt(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, _, _ = _run_retry(monkeypatch, capsys, "etherlan", SAVED_RED, ["ok"])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 1
    assert verify_sleeps == []


# --- Wi-Fi Warning confirmation evidence -----------------------------------------------------------

WIFI_PAGES = ["wconfig", "wconfig_unified"]
WARNING_WITH_CONTINUE = (
    '<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/{page}.ha">'
    '<input type="hidden" name="nonce" value="a1b2c3"><input type="submit" name="Continue" value="Continue">'
    "</form></body></html>"
)
WARNING_WITHOUT_CONTINUE = (
    '<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/wifiwarn_advanced.ha">'
    '<input type="submit" name="Cancel" value="Cancel"></form></body></html>'
)


def _warning_flow(page, warning):
    """Save POST answers 302 -> wifiwarn_advanced.ha; `warning(request)` answers the warning page GET.
    A Continue POST answers 302 -> <page>.ha and the page then shows Changes saved with the new value."""
    state = {"saved": False}

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body or "")
        if request.method == "POST":
            if "Continue" in body:
                state["saved"] = True
                return html("", status=302, headers={"location": f"/cgi-bin/{page}.ha"})
            return html("", status=302, headers={"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if path.startswith("wifiwarn"):
            return warning(request)
        if state["saved"]:
            return html(form(page, "new", "setting", SAVED_RED))
        return html(form(page, "old", "setting"))

    return handle


def _run_wifi(monkeypatch, capsys, page, warning):
    client, wire = client_with(_warning_flow(page, warning))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    token = page.upper().replace("_", "-")
    code = cli.main(["set", page, "setting=new", "--commit", "--confirm", token, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("page", WIFI_PAGES)
def test_wifi_warning_page_http_error_keeps_the_save_posts_redirect_evidence(clock, tmp_env, capsys, monkeypatch, page):
    code, output, wire = _run_wifi(monkeypatch, capsys, page, lambda r: html("<html>Error</html>", status=500))
    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    # The save POST was sent and answered (the redirect); only Continue was never posted.
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert "Continue was not posted" in output["warning"] and "sent once" in output["warning"]
    assert output["statusCode"] == 302
    assert output["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "wifiwarn_advanced" in output["warning"] and "HTTP 500" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("page", WIFI_PAGES)
def test_wifi_warning_page_without_continue_keeps_the_save_posts_redirect_evidence(
    clock, tmp_env, capsys, monkeypatch, page
):
    code, output, wire = _run_wifi(monkeypatch, capsys, page, lambda r: html(WARNING_WITHOUT_CONTINUE))
    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert "Continue was not posted" in output["warning"]
    assert output["statusCode"] == 302
    assert output["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "no Continue button" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("argv", [
    ["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG"],
    ["submit", "wconfig", "Save", "--commit", "--confirm", "WCONFIG"],
])
def test_wifi_warning_continue_nonce_read_failure_keeps_the_save_post_as_sent(
    clock, tmp_env, capsys, monkeypatch, argv
):
    """Continue's own nonce read (the warning page, read again for its form nonce) fails, so Continue
    never leaves: the save POST was still sent and answered, and `set` (restore engine) and `submit`
    (generic confirmation) report it the same way."""
    warning_reads = {"count": 0}

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if path.startswith("wifiwarn"):
            warning_reads["count"] += 1
            if warning_reads["count"] > 1:
                return html("<html>Error</html>", status=500)
            return html(WARNING_WITH_CONTINUE.format(page="wconfig"))
        return html(form("wconfig", "old", "setting"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 302 and output["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "HTTP 500" in output["warning"] and "Continue was not posted" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("page", WIFI_PAGES)
def test_wifi_warning_happy_path_evidence_is_unchanged(clock, tmp_env, capsys, monkeypatch, page):
    """Pinned from the pre-change output: the final evidence is the Continue POST's 302 -> <page>.ha."""
    code, output, wire = _run_wifi(monkeypatch, capsys, page, lambda r: html(WARNING_WITH_CONTINUE.format(page=page)))
    assert code == 0 and output["committed"] is True and output["outcome"] == "applied"
    assert output["writeAttempted"] is True and output["acknowledgementObserved"] is True
    assert output["verified"] is True
    assert output["statusCode"] == 302 and output["location"] == f"/cgi-bin/{page}.ha"
    assert [f"{r.method} {r.url.rsplit('/', 1)[-1]}" for r in wire.requests if r.method == "POST"] == [
        f"POST {page}.ha", f"POST {page}.ha"
    ]


# --- session pool full at the real entry point -------------------------------------------------------

from bgwcli.client import session_pool_full_error  # noqa: E402 - grouped with the tests that use it

POOL_META = {"waited_ms": 2300, "retry_count": 4}
POOL_PAGES = {
    "wconfig": PAGES["wconfig"],
    "dhcpserver": PAGES["dhcpserver"],
    "etherlan": ("setting", "old", "new", "ETHERLAN"),
}


def _assert_pool_full_block(code, output):
    """What main() emits for a full session pool, with the metadata the helper-level tests pinned."""
    assert code == 2
    assert output["ok"] is False and output["page"] == "login"
    assert output["sessionPoolFull"] is True
    assert output["waitedMs"] == 2300 and output["retryCount"] == 4
    assert output["error"]


def _run_pool(monkeypatch, capsys, argv, handle):
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("when", ["post", "ack"])
@pytest.mark.parametrize("page", list(POOL_PAGES))
def test_set_reports_pool_full_metadata_when_the_save_cannot_be_confirmed(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page, when
):
    field, old, new, token = POOL_PAGES[page]
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            if when == "post":
                raise session_pool_full_error(**POOL_META)
            state["posted"] = True
            return html("", status=302, headers={"location": f"/cgi-bin/{page}.ha"})
        if state["posted"]:
            raise session_pool_full_error(**POOL_META)  # the acknowledgement wait hits the full pool
        return html(form(page, old, field))

    code, output, wire = _run_pool(
        monkeypatch, capsys, ["set", page, f"{field}={new}", "--commit", "--confirm", token], handle
    )
    _assert_pool_full_block(code, output)
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert verify_sleeps == []


@pytest.mark.parametrize("page", list(POOL_PAGES))
def test_set_pool_full_during_the_verification_re_read_is_not_retried(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    """`Changes saved` was observed, then the re-read hits a full pool. Pool-full is never retried.
    main() answers with its pool-full block, which keeps the acknowledged write's evidence."""
    field, old, new, token = POOL_PAGES[page]
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(form(page, new, field, SAVED_RED))
        if state["posted"]:
            raise session_pool_full_error(**POOL_META)
        return html(form(page, old, field))

    code, output, wire = _run_pool(
        monkeypatch, capsys, ["set", page, f"{field}={new}", "--commit", "--confirm", token], handle
    )
    _assert_pool_full_block(code, output)
    assert verify_sleeps == []
    assert len([r for r in wire.requests if r.method == "GET" and state["posted"]]) >= 1
    assert output["acknowledgementObserved"] is True and output["writePerformed"] is True
    assert output["committed"] is True
    assert output["error"] == (
        "Router web session pool is full. Changes saved was observed. "
        "The change was sent once; verify the gateway state before retrying."
    )


# The warning page carries its own nonce W for the Continue form (action wconfig.ha); the wconfig
# page itself carries a different nonce P (abc123, from save_helpers.form).
WARNING_NONCE = "deadbeef77"
WCONFIG_PAGE_NONCE = "abc123"
WARNING_OWN_NONCE = (
    '<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/wconfig.ha">'
    f'<input type="hidden" name="nonce" value="{WARNING_NONCE}"><input type="submit" name="Continue" value="Continue">'
    '</form><form method="post" action="/cgi-bin/wifiwarn_advanced.ha">'
    '<input type="hidden" name="nonce" value="cafe0042"><input type="submit" name="Cancel" value="Cancel">'
    "</form></body></html>"
)
WARNING_CONTINUE_WITHOUT_NONCE = (
    '<html><body><h1>Wi-Fi Warning</h1><form method="post" action="/cgi-bin/wconfig.ha">'
    '<input type="submit" name="Continue" value="Continue"></form>'
    '<form method="post" action="/cgi-bin/wifiwarn_advanced.ha">'
    '<input type="hidden" name="nonce" value="cafe0042"><input type="submit" name="Cancel" value="Cancel">'
    "</form></body></html>"
)


def _nonce_flow(warning_html):
    """Save -> 302 to the warning page; Continue -> 302 to wconfig and then Changes saved. Records POSTs."""
    state = {"saved": False}
    posts: list[tuple[str, str]] = []

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body or "")
        if request.method == "POST":
            posts.append((path, body))
            if "Continue" in body:
                state["saved"] = True
                return html("", status=302, headers={"location": "/cgi-bin/wconfig.ha"})
            return html("", status=302, headers={"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if path.startswith("wifiwarn"):
            return html(warning_html)
        if state["saved"]:
            return html(form("wconfig", "new", "setting", SAVED_RED))
        return html(form("wconfig", "old", "setting"))

    return handle, posts


@pytest.mark.parametrize("argv", [
    ["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG"],
    ["submit", "wconfig", "Save", "setting=new", "--commit", "--confirm", "WCONFIG"],
])
def test_wifi_warning_continue_carries_the_warning_pages_own_nonce(clock, tmp_env, capsys, monkeypatch, argv):
    handle, posts = _nonce_flow(WARNING_OWN_NONCE)
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 0 and output["outcome"] == "applied" and output["committed"] is True
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert [p for p, _ in posts] == ["wconfig.ha", "wconfig.ha"]  # Save, then Continue: exactly two POSTs
    assert f"nonce={WCONFIG_PAGE_NONCE}" in posts[0][1]
    assert "Continue=Continue" in posts[1][1]
    assert f"nonce={WARNING_NONCE}" in posts[1][1].split("&")
    assert WCONFIG_PAGE_NONCE not in posts[1][1]
    assert sum(r.method == "POST" for r in wire.requests) == 2


def test_wifi_warning_continue_carries_the_warning_pages_own_nonce_in_a_restore_step(clock, tmp_env):
    handle, posts = _nonce_flow(WARNING_OWN_NONCE)
    client, wire = client_with(handle)
    step = restore.RestoreStep(
        1, "form", "wconfig", "save wconfig", button="Save", raw_payload={"setting": "new", "Save": "Save"}
    )
    execution = restore.execute_restore(client, [step])

    assert [s.status for s in execution.steps] == ["applied"], execution.steps[0].error
    assert execution.steps[0].write_attempted is True and execution.steps[0].write_response_received is True
    assert [p for p, _ in posts] == ["wconfig.ha", "wconfig.ha"]
    assert f"nonce={WARNING_NONCE}" in posts[1][1].split("&")
    assert WCONFIG_PAGE_NONCE not in posts[1][1]
    assert sum(r.method == "POST" for r in wire.requests) == 2


@pytest.mark.parametrize("argv", [
    ["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG"],
    ["submit", "wconfig", "Save", "setting=new", "--commit", "--confirm", "WCONFIG"],
])
def test_wifi_warning_continue_form_without_a_nonce_is_refused_before_continue_is_posted(
    clock, tmp_env, capsys, monkeypatch, argv
):
    handle, posts = _nonce_flow(WARNING_CONTINUE_WITHOUT_NONCE)
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2 and output["committed"] is False and output["outcome"] == "failed", json.dumps(output)
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["statusCode"] == 302 and output["location"] == "/cgi-bin/wifiwarn_advanced.ha"
    assert "no write nonce found for wconfig.ha" in output["warning"] and "Continue was not posted" in output["warning"]
    assert "nothing was sent" not in output["warning"]  # the Save POST was sent and answered
    assert "gateway discards an unconfirmed Wi-Fi change" in output["warning"]
    assert [p for p, _ in posts] == ["wconfig.ha"]  # only the Save POST left
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_action_reports_pool_full_metadata_when_the_warning_page_cannot_be_read(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    """The action path (_run_action -> _post_and_confirm): the scan POST answers with the Wi-Fi Warning
    redirect and the warning page read hits a full pool."""
    scan_form = (
        '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="abc123">'
        '<input type="text" name="setting" value="old">'
        '<input type="submit" name="chanscan5" value="Find Best Channel"></form>'
    )

    def handle(request, n):
        path = request.url.rsplit("/", 1)[-1]
        if request.method == "POST":
            return html("", status=302, headers={"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if path.startswith("wifiwarn"):
            raise session_pool_full_error(**POOL_META)
        return html(scan_form)

    code, output, wire = _run_pool(
        monkeypatch, capsys, ["action", "find-best-channel-5", "--commit", "--confirm", "CHANSCAN"], handle
    )
    _assert_pool_full_block(code, output)
    assert [f"{r.method} {r.url.rsplit('/', 1)[-1]}" for r in wire.requests][-2:] == [
        "POST wconfig.ha", "GET wifiwarn_advanced.ha"
    ]
    assert verify_sleeps == []


# --- connection errors during a Wi-Fi save are structured like every other page -----------------------


def _connection_loss(page, field, old, new, *, during):
    """`during="post"`: the save POST itself raises; `during="ack"`: the POST is answered 302 and the
    acknowledgement reads raise."""
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            if during == "post":
                raise TimeoutError("router unreachable while posting")
            state["posted"] = True
            return html("", status=302, headers={"location": f"/cgi-bin/{page}.ha"})
        if state["posted"]:
            raise TimeoutError("router unreachable while confirming")
        return html(form(page, old, field))

    return handle


def _run_conn(monkeypatch, capsys, page, during):
    field, old, new, token = {**RETRY_PAGES, "wconfig_unified": ("setting", "old", "new", "WCONFIG-UNIFIED")}[page]
    client, wire = client_with(_connection_loss(page, field, old, new, during=during))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", page, f"{field}={new}", "--commit", "--confirm", token, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


@pytest.mark.parametrize("page", WIFI_PAGES)
def test_wifi_connection_error_on_the_save_post_is_a_structured_failure(clock, tmp_env, capsys, monkeypatch, page):
    code, output, wire = _run_conn(monkeypatch, capsys, page, "post")
    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    # The client reports the loss as a connection timeout against the page URL.
    assert f"Timed out connecting to http://router.local/cgi-bin/{page}.ha" in output["warning"]
    assert output.get("acknowledgementObserved") is not True
    assert sum(r.method == "POST" for r in wire.requests) == 1
    # Pinned from restore's write evidence: the transport was invoked for the POST, so the write counts
    # as attempted (its delivery is unknown) and no response was received.
    assert output["writeAttempted"] is True


@pytest.mark.parametrize("page", WIFI_PAGES)
def test_wifi_connection_error_while_confirming_keeps_the_sent_once_guidance(
    clock, tmp_env, capsys, monkeypatch, page
):
    code, output, wire = _run_conn(monkeypatch, capsys, page, "ack")
    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output.get("acknowledgementObserved") is not True
    assert f"Timed out connecting to http://router.local/cgi-bin/{page}.ha" in output["warning"]
    assert "sent once" in output["warning"] and "verify the gateway state" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_wifi_and_generic_connection_errors_while_confirming_share_exit_and_json_shape(
    clock, tmp_env, capsys, monkeypatch
):
    results = {page: _run_conn(monkeypatch, capsys, page, "ack") for page in ("wconfig", "etherlan")}
    (code_w, out_w, _), (code_e, out_e, _) = results["wconfig"], results["etherlan"]
    assert code_w == code_e == 2
    assert set(out_w) == set(out_e), (sorted(out_w), sorted(out_e))
    for key in ("committed", "outcome", "writeAttempted", "dryRun", "guarded"):
        assert out_w[key] == out_e[key]


# --- an attempted-but-unavailable re-read after Changes saved is "could not answer" ---------------------

from bgwcli.save_result import decide_save_result  # noqa: E402 - grouped with the tests that use it


@pytest.mark.parametrize("page", list(RETRY_PAGES))
def test_connection_loss_on_every_re_read_attempt_exits_2_but_keeps_committed(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    code, output, _, reads = _run_retry(monkeypatch, capsys, page, SAVED_RED, ["drop", "drop", "drop"])
    assert code == 2
    assert output["committed"] is True and output["outcome"] == "applied"
    assert output.get("verified") is None and output.get("mismatches") is None
    assert output["verifyAttempts"] == 3
    assert "3 attempts" in output["warning"] and "Timed out connecting" in output["warning"]
    assert verify_sleeps == [2.0, 4.0]
    assert len(reads) == 2 + 3


def test_applied_submit_without_a_re_read_stays_exit_0(clock, tmp_env, capsys, monkeypatch):
    """Pinned: a re-read skipped by design (submit) is not an unavailable re-read."""
    client, wire = client_with(lambda request, n: html(
        form("etherlan", "old") if n <= 2 else form("etherlan", "new", "setting", SAVED_RED)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["submit", "etherlan", "Save", "setting=new", "--commit", "--confirm", "ETHERLAN", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["committed"] is True and output["outcome"] == "applied"
    assert "verified" not in output and "verifyAttempts" not in output and "warning" not in output
    assert [r.method for r in wire.requests][-1] == "POST", "no re-read after an applied submit"


@pytest.mark.parametrize("verify_warning,expected", [
    ("could not re-read etherlan to verify the change after 3 attempts: HTTP 500", 2),
    (None, 0),
])
def test_decide_save_result_applied_unverified_depends_on_whether_a_re_read_was_attempted(verify_warning, expected):
    decision = decide_save_result("applied", None, None, None, None, requested=True, verify_warning=verify_warning)
    assert decision.exit_code == expected
    assert decision.outcome == "applied" and decision.committed is True


def test_exhausted_re_read_uses_the_shared_sent_once_guidance(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, _, _ = _run_retry(monkeypatch, capsys, "etherlan", SAVED_RED, ["500", "500", "500"])
    assert code == 2
    assert output["warning"].endswith(restore.SENT_ONCE_GUIDANCE)
    assert restore.SENT_ONCE_GUIDANCE.startswith("The change was sent once; ")
    assert restore.SENT_ONCE_GUIDANCE.endswith(restore.VERIFY_BEFORE_RETRYING)


@pytest.mark.parametrize(("status", "retried"), [(408, True), (429, True), (500, True), (503, True), (501, False),
                                                 (404, False)])
def test_re_read_and_acknowledgement_poll_share_one_retry_rule(status, retried):
    assert restore.is_transient_verification_status(status) is retried
    assert (status in restore.TRANSIENT_VERIFICATION_STATUSES) is retried


def test_re_read_does_not_retry_a_non_transient_server_error(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, _, _ = _run_retry(monkeypatch, capsys, "etherlan", SAVED_RED, ["501"])
    assert code == 2 and output["verifyAttempts"] == 1 and "HTTP 501" in output["warning"]
    assert verify_sleeps == []


# --- a rejection banner inline in a 200 answer on a Save-less page -------------------------------------

DIAG_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value="{value}">'
    '<input type="submit" name="Ping" value="Ping"></form>'
)


def _diag_client(monkeypatch, post_body):
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(post_body, status=200)
        return html(DIAG_FORM.format(value="new" if state["posted"] else "old"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    return wire


def test_submit_on_a_save_less_page_reports_an_inline_200_rejection_banner(clock, tmp_env, capsys, monkeypatch):
    wire = _diag_client(monkeypatch, ERROR + DIAG_FORM.format(value="old"))
    code = cli.main(["submit", "diag", "Ping", "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["warning"].startswith("router rejected the change: A required setting is empty")
    assert output["writeAttempted"] is True and output["acknowledgementObserved"] is False
    assert output["statusCode"] == 200
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_set_on_a_save_less_page_reports_an_inline_200_rejection_not_a_mismatch(clock, tmp_env, capsys, monkeypatch):
    wire = _diag_client(monkeypatch, ERROR + DIAG_FORM.format(value="new"))
    code = cli.main(["set", "diag", "WebAddress=new", "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["warning"].startswith("router rejected the change: A required setting is empty")
    assert "mismatches" not in output and "verified" not in output
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("operation", ["submit", "set"])
def test_save_less_page_200_without_a_banner_is_still_applied(clock, tmp_env, capsys, monkeypatch, operation):
    wire = _diag_client(monkeypatch, DIAG_FORM.format(value="new"))
    argv = (["submit", "diag", "Ping"] if operation == "submit" else ["set", "diag", "WebAddress=new"])
    code = cli.main([*argv, "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["committed"] is True and output["outcome"] == "applied"
    reads_after_post = wire.requests[[r.method for r in wire.requests].index("POST") + 1:]
    if operation == "set":
        assert output["verified"] is True and len(reads_after_post) >= 1  # re-read and verified
    else:
        assert "verified" not in output and reads_after_post == []  # submit is not re-read


# --- the inline 200 answer on a Save-less page carries the whole notification ---------------------------

NO_CHANGE_PLAIN = '<div id="error-message-text">No changes detected. Save not performed.</div>'


@pytest.mark.parametrize("operation", ["submit", "set"])
def test_save_less_page_200_with_changes_saved_is_an_observed_acknowledgement(
    clock, tmp_env, capsys, monkeypatch, operation
):
    wire = _diag_client(monkeypatch, SAVED_RED + DIAG_FORM.format(value="new"))
    argv = (["submit", "diag", "Ping"] if operation == "submit" else ["set", "diag", "WebAddress=new"])
    code = cli.main([*argv, "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["committed"] is True and output["outcome"] == "applied"
    assert output["acknowledgementObserved"] is True and output["writePerformed"] is True
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


def _no_change_diag_client(monkeypatch, banner, live_after):
    def handle(request, n):
        if request.method == "POST":
            return html(banner + DIAG_FORM.format(value="old"), status=200)
        return html(DIAG_FORM.format(value=live_after if any(r.method == "POST" for r in wire.requests) else "old"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    return wire


@pytest.mark.parametrize("banner", [NO_CHANGE, NO_CHANGE_PLAIN], ids=["with-icon", "plain"])
def test_save_less_page_200_with_no_changes_detected_and_nothing_requested_is_unchanged(
    clock, tmp_env, capsys, monkeypatch, banner
):
    wire = _no_change_diag_client(monkeypatch, banner, "old")
    code = cli.main(["submit", "diag", "Ping", "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["committed"] is False and output["outcome"] == "unchanged"
    assert output["writePerformed"] is False and output["writeAttempted"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("banner", [NO_CHANGE, NO_CHANGE_PLAIN], ids=["with-icon", "plain"])
@pytest.mark.parametrize("live_after,expected", [("old", (1, "failed", False)), ("new", (0, "unchanged", True))])
def test_save_less_page_200_with_no_changes_detected_and_requested_values_is_decided_by_the_re_read(
    clock, tmp_env, capsys, monkeypatch, banner, live_after, expected
):
    wire = _no_change_diag_client(monkeypatch, banner, live_after)
    code = cli.main(["set", "diag", "WebAddress=new", "--commit", "--confirm", "DIAG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert (code, output["outcome"], output["verified"]) == expected
    assert output["committed"] is False and output["writePerformed"] is False
    assert "router rejected" not in (output.get("warning") or "")
    assert sum(r.method == "POST" for r in wire.requests) == 1
