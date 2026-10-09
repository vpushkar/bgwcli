"""Write evidence reported by the generic save path when the save itself fails.

Drives `cli.main([... "--json"])` over the real client with an in-memory transport, so the
transport's write counters decide whether a write was sent and whether it was answered.
"""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli


def _run(monkeypatch, capsys, handle, argv=("set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN")):
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


def test_http_error_on_the_nonce_read_reports_no_write_sent(clock, tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if n == 2:  # the nonce read that precedes the POST
            return html("<html>Internal Server Error</html>", status=500)
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is False
    assert output["writeResponseReceived"] is False
    assert "sent once" not in output["warning"]
    assert "attempted once" not in output["warning"]
    assert "before any write was sent" in output["warning"]
    assert not any(r.method == "POST" for r in wire.requests)


def test_http_error_answering_the_post_reports_the_response_and_status(clock, tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if request.method == "POST":
            return html("<html>Internal Server Error</html>", status=500)
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True
    assert output["writeResponseReceived"] is True
    assert output["acknowledgementObserved"] is False
    assert output["statusCode"] == 500
    assert "HTTP 500" in output["warning"] and "verify the gateway state" in output["warning"]
    assert "final POST state is unknown" not in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_connection_error_on_the_post_is_structured_json(clock, tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if request.method == "POST":
            raise TimeoutError("router unreachable while posting")
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True
    assert output["writeResponseReceived"] is False
    assert output["acknowledgementObserved"] is False
    assert "Timed out connecting" in output["warning"]
    assert "verify the gateway state" in output["warning"]
    assert "statusCode" not in output
    assert sum(r.method == "POST" for r in wire.requests) == 1


# --- Save-less pages: the POST answer is the only evidence, the re-read decides -----------------------

SAVELESS_ARGV = ("set", "diag", "WebAddress=example.test", "--commit", "--confirm", "DIAG")


def _saveless_form(value: str) -> str:
    return (
        '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="WebAddress" value="{value}">'
        '<input type="submit" name="Ping" value="Ping"></form>'
    )


def _saveless(after_post):
    """Plan read + nonce read serve the old form, the POST answers 302 -> diag.ha, and every GET after
    the POST is answered by `after_post()`."""
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/diag.ha"})
        if state["posted"]:
            return after_post()
        return html(_saveless_form("old.test"))

    return handle


def test_saveless_set_whose_redirect_answer_cannot_be_read_exits_2_with_write_evidence(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    # The 302 target carries the gateway's answer; an HTTP 500 there leaves the write unanswered:
    # a failed step with the POST's evidence (exit 2), not an applied write to re-read.
    code, output, wire = _run(
        monkeypatch,
        capsys,
        _saveless(lambda: html("<html>Internal Server Error</html>", status=500)),
        SAVELESS_ARGV,
    )

    assert code == 2
    assert output["outcome"] == "failed" and output["committed"] is False
    assert "HTTP 500" in output["warning"]
    assert output["writeAttempted"] is True
    assert output["writeResponseReceived"] is True
    assert output["acknowledgementObserved"] is False
    assert output["statusCode"] == 302
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_saveless_set_with_a_verified_re_read_exits_0_and_keeps_the_plan_warning(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, output, _ = _run(monkeypatch, capsys, _saveless(lambda: html(_saveless_form("example.test"))), SAVELESS_ARGV)

    assert code == 0
    assert output["outcome"] == "applied" and output["committed"] is True
    assert output["verified"] is True and output["verifyAttempts"] == 1
    # The mutation plan's warning surfaces but never changes the exit code.
    assert "has no Save button" in output["warning"]
    assert "could not re-read" not in output["warning"]
    assert output["writeAttempted"] is True and output["acknowledgementObserved"] is False
    assert verify_sleeps == []


# --- authentication failures while polling for "Changes saved" ---------------------------------------


def _poll_answers(answer):
    """Plan read + nonce read serve the form, the POST answers 302 -> etherlan.ha, and every GET after
    the POST is answered by `answer()`."""
    state = {"posted": False}

    def handle(request, n):
        if request.url.endswith("/login.ha"):
            return html('<title>Login</title><form><input name="nonce" value="abc123"></form>')
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": "/cgi-bin/etherlan.ha"})
        if state["posted"]:
            return answer()
        return html(form("etherlan", "old"))

    return handle


def test_page_level_403_while_polling_for_the_acknowledgement_is_a_structured_failure(
    clock, tmp_env, capsys, monkeypatch
):
    code, output, wire = _run(monkeypatch, capsys, _poll_answers(lambda: html("<html>Forbidden</html>", status=403)))

    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output.get("acknowledgementObserved") is False
    assert "HTTP 403" in output["warning"] and "sent once" in output["warning"]
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_session_wide_401_while_polling_for_the_acknowledgement_still_raises(clock, tmp_env, capsys, monkeypatch):
    client, wire = client_with(
        _poll_answers(lambda: html('<title>Login</title><form><input name="password"></form>', status=401))
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN", "--json"])
    captured = capsys.readouterr()

    assert code == 2
    error = json.loads(captured.out)
    assert error["ok"] is False and error["exitCode"] == 2 and error["errorType"] == "RouterAuthError"
    assert "HTTP 401" in error["error"] and "sent once" in error["error"]
    assert error["writeAttempted"] is True
    assert "HTTP 401" in captured.err and "sent once" in captured.err
    assert sum(r.method == "POST" and not r.url.endswith("/login.ha") for r in wire.requests) == 1


def test_error_banner_after_a_saveless_redirect_is_a_structured_rejection(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    banner = (
        '<img id="error-message-icon" src="/images/icon_error.png">'
        '<div id="error-message-text">A required setting is empty</div>'
    )
    code, output, wire = _run(
        monkeypatch, capsys, _saveless(lambda: html(banner + _saveless_form("old.test"))), SAVELESS_ARGV
    )

    assert code == 1
    assert output["outcome"] == "failed" and output["committed"] is False
    assert output["warning"].startswith("router rejected the change: ")
    assert "A required setting is empty" in output["warning"]
    assert output["writeAttempted"] is True and output["writeResponseReceived"] is True
    assert output["acknowledgementObserved"] is False
    assert output["statusCode"] == 302 and output["location"] == "/cgi-bin/diag.ha"
    assert "verified" not in output, "a rejection is final; no re-read decides it"
    assert verify_sleeps == []
    assert sum(r.method == "POST" for r in wire.requests) == 1


# --- raised pool-full / session-wide errors say whether the write was sent ---------------------------

from bgwcli.client import session_pool_full_error  # noqa: E402 - grouped with the tests that use it


@pytest.mark.parametrize("when", ["nonce", "post"])
def test_pool_full_on_the_save_says_whether_the_write_was_sent(clock, tmp_env, capsys, monkeypatch, when):
    def handle(request, n):
        if when == "nonce" and n == 2:
            raise session_pool_full_error()
        if request.method == "POST":
            raise session_pool_full_error()
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, handle)

    assert code == 2 and output["sessionPoolFull"] is True
    if when == "nonce":
        assert "before any write was sent" in output["error"]
        assert "attempted once" not in output["error"] and "sent once" not in output["error"]
        assert not any(r.method == "POST" for r in wire.requests)
    else:
        assert "attempted once" in output["error"] and "verify the gateway state" in output["error"]
        assert "before any write was sent" not in output["error"]


def test_session_wide_auth_error_before_the_post_says_nothing_was_sent(clock, tmp_env, capsys, monkeypatch):
    def handle(request, n):
        if n == 2:  # the nonce read bounces to the Login page with 401: the session is gone
            return html('<title>Login</title><form><input name="password"></form>', status=401)
        return html(form("etherlan", "old"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN", "--json"])
    err = capsys.readouterr().err

    assert code == 2
    assert "HTTP 401" in err and "before any write was sent" in err
    assert "attempted once" not in err
    assert not any(r.method == "POST" for r in wire.requests)


def test_pool_full_answering_the_sent_write_reports_the_write_in_json(clock, tmp_env, capsys, monkeypatch):
    """The gateway answered the save POST with its pool-full page: the write was sent, so the
    pool-full JSON object says so next to the sent-once guidance."""
    def handle(request, n):
        if request.method == "POST":
            return html("<title>Login</title><p>All web server sessions are in use</p>")
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, handle)

    assert code == 2
    assert output["sessionPoolFull"] is True and output["exitCode"] == 2
    assert output["writeAttempted"] is True
    assert "sent once" in output["error"]
    assert sum(r.method == "POST" for r in wire.requests) == 1
