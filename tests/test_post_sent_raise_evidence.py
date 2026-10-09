"""A pool-full or lost-session error raised after an ANSWERED POST carries `writeResponseReceived: true`.

Only the answer is evidenced: no commit, write-performed or acknowledgement key appears, and a POST
that was never answered (the connection failed mid-POST) keeps `writeAttempted: true` alone.
Drives `cli.main([... "--json"])` over the real client with an in-memory transport.
"""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli, diagnostics
from bgwcli.client import session_pool_full_error

POOL_FULL = "<title>Login</title><p>All web server sessions are in use</p>"
LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
UNOBSERVED = ("committed", "writePerformed", "acknowledgementObserved")

SAVELESS_ARGV = ["set", "diag", "WebAddress=example.test", "--commit", "--confirm", "DIAG"]
ACTION_ARGV = ["action", "detect-wifi-congestion-2.4", "--commit", "--confirm", "CONGESTION-2.4"]
DIAGNOSTIC_ARGV = ["diagnostics", "ping", "example.com", "--commit", "--confirm", "DIAG"]
GENERIC_ARGV = ["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN"]

SAVELESS_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value="old.test">'
    '<input type="submit" name="Ping" value="Ping"></form>'
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


def _run(monkeypatch, capsys, argv, handle):
    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


def _answered_then(page_before: str, after_post, *, redirect: str = "/cgi-bin/diag.ha"):
    """GETs serve `page_before` until the POST, which answers 302; every later GET is `after_post(gets)`."""
    state = {"posted": False, "gets_after": 0}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", status=302, headers={"location": redirect})
        if state["posted"]:
            state["gets_after"] += 1
            return after_post(state["gets_after"])
        return html(page_before)

    return handle


def _assert_answered_post(code, output, wire):
    assert code == 2
    assert output["writeAttempted"] is True
    assert output["writeResponseReceived"] is True
    for key in UNOBSERVED:
        assert key not in output
    assert sum(r.method == "POST" for r in wire.requests) == 1


@pytest.mark.parametrize("fault", ["pool-full", "session-lost"])
def test_saveless_set_whose_redirect_answer_read_faults_carries_the_response_evidence(
    clock, tmp_env, capsys, monkeypatch, fault
):
    answer = html(POOL_FULL) if fault == "pool-full" else html(LOGIN, status=403)
    handle = _answered_then(SAVELESS_FORM, lambda _n: answer)

    code, output, wire = _run(monkeypatch, capsys, SAVELESS_ARGV, handle)

    _assert_answered_post(code, output, wire)
    assert output["ok"] is False and "sent once" in output["error"]
    assert output.get("sessionPoolFull") is (True if fault == "pool-full" else None)


def test_action_whose_answer_read_hits_pool_full_carries_the_response_evidence(
    clock, tmp_env, capsys, monkeypatch
):
    handle = _answered_then(CONGESTION_FORM, lambda _n: html(POOL_FULL), redirect="/cgi-bin/lanstatistics.ha")

    code, output, wire = _run(monkeypatch, capsys, ACTION_ARGV, handle)

    _assert_answered_post(code, output, wire)
    assert output["sessionPoolFull"] is True and "sent once" in output["error"]


def test_generic_set_answered_by_the_pool_full_body_carries_the_response_evidence(
    clock, tmp_env, capsys, monkeypatch
):
    def handle(request, n):
        if request.method == "POST":
            return html(POOL_FULL)
        return html(form("etherlan", "old"))

    code, output, wire = _run(monkeypatch, capsys, GENERIC_ARGV, handle)

    _assert_answered_post(code, output, wire)
    assert output["sessionPoolFull"] is True and "sent once" in output["error"]


@pytest.mark.parametrize("fault", ["pool-full", "session-lost"])
def test_diagnostic_poll_fault_carries_the_response_evidence(clock, tmp_env, capsys, monkeypatch, fault):
    monkeypatch.setattr(diagnostics, "_sleep", lambda seconds: None)
    answer = html(POOL_FULL) if fault == "pool-full" else html(LOGIN, status=403)
    # The first read after the POST is the redirect target (an empty result page); the poll then faults.
    handle = _answered_then(DIAG_FORM, lambda n: html(DIAG_FORM) if n == 1 else answer)

    code, output, wire = _run(monkeypatch, capsys, DIAGNOSTIC_ARGV, handle)

    _assert_answered_post(code, output, wire)
    assert "sent once" in output["error"]


@pytest.mark.parametrize(
    ("argv", "page"),
    [(GENERIC_ARGV, form("etherlan", "old")), (SAVELESS_ARGV, SAVELESS_FORM)],
    ids=["generic", "saveless"],
)
def test_a_post_that_was_never_answered_keeps_the_response_unknown(clock, tmp_env, capsys, monkeypatch, argv, page):
    def handle(request, n):
        if request.method == "POST":
            raise session_pool_full_error()
        return html(page)

    code, output, wire = _run(monkeypatch, capsys, argv, handle)

    assert code == 2 and output["writeAttempted"] is True
    assert "writeResponseReceived" not in output
    for key in UNOBSERVED:
        assert key not in output
    assert sum(r.method == "POST" for r in wire.requests) == 1
