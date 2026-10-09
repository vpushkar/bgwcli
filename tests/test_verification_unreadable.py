"""An acknowledged save whose verification page cannot be read is applied but unverifiable (exit 2),
never a mismatch against a fabricated `<absent>` live value. A readable page that lacks the field,
or shows another value, is still a mismatch (exit 1)."""

from __future__ import annotations

import json

import pytest
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
NOT_FOUND = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"
LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
NO_FIELD = (
    '<form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="other" value="x"><input type="submit" name="Save" value="Save"></form>'
)
# Tables nested past the parser's bound: the page is cut although it still shows the live control.
MANY_TEXTAREAS = form("dosprotect", "new", "setting") + "<table>" * 300


def _run(monkeypatch, capsys, rereads, *, page="dosprotect", token="DOSPROTECT"):
    """Plan and nonce reads, one acknowledged POST, then one scripted body per verification re-read.
    A body of None means the matching live form."""
    script = list(rereads)
    state = {"posted": False}

    def handle(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html(form(page, "new", "setting", SAVED_RED))
        if not state["posted"]:
            return html(form(page, "old", "setting"))
        assert script, "unexpected extra verification read"
        body = script.pop(0)
        return html(form(page, "new", "setting") if body is None else body)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", page, "setting=new", "--commit", "--confirm", token, "--json"])
    output = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha") for r in wire.requests)
    return code, output, posts, script


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (PLEASE_WAIT, "without any form controls"),
        (MANY_TEXTAREAS, "parser's bounds"),
        (NOT_FOUND, "Page not found"),
    ],
    ids=["please-wait", "truncated", "not-found"],
)
def test_unreadable_verification_page_is_applied_but_unverifiable(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, body, reason
):
    code, output, posts, _ = _run(monkeypatch, capsys, [body, body, body])

    assert code == 2
    assert output["committed"] is True and output["outcome"] == "applied"
    assert output["writePerformed"] is True and output.get("verified") is None
    assert output.get("mismatches") is None
    # Page-not-found is rejected by fetch_parsed_page as final; the unreadable-page reasons are retried.
    assert output["verifyAttempts"] == (1 if reason == "Page not found" else 3)
    assert reason in output["warning"] and "<absent>" not in json.dumps(output)
    assert posts == 1


def test_please_wait_is_retried_with_the_existing_delays(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, posts, _ = _run(monkeypatch, capsys, [PLEASE_WAIT, PLEASE_WAIT, PLEASE_WAIT])
    assert code == 2 and output["verifyAttempts"] == 3
    assert "3 attempts" in output["warning"]
    assert verify_sleeps == [2.0, 4.0]
    assert posts == 1


def test_please_wait_then_readable_match_verifies(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, posts, _ = _run(monkeypatch, capsys, [PLEASE_WAIT, None])
    assert code == 0 and output["committed"] is True and output["verified"] is True
    assert output["verifyAttempts"] == 2
    assert verify_sleeps == [2.0]
    assert posts == 1


def test_truncated_then_readable_match_verifies(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, posts, _ = _run(monkeypatch, capsys, [MANY_TEXTAREAS, PLEASE_WAIT, None])
    assert code == 0 and output["verified"] is True and output["verifyAttempts"] == 3
    assert posts == 1


def test_login_page_on_re_read_is_unverifiable_not_a_mismatch(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, posts, _ = _run(monkeypatch, capsys, [LOGIN, LOGIN, LOGIN])
    assert code == 2 and output["committed"] is True and output.get("verified") is None
    assert output.get("mismatches") is None and "<absent>" not in json.dumps(output)
    assert "login" in output["warning"].lower()
    assert posts == 1


def test_readable_page_without_the_field_is_still_a_mismatch(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, output, posts, _ = _run(monkeypatch, capsys, [NO_FIELD])
    assert code == 1 and output["committed"] is True and output["verified"] is False
    assert output["mismatches"] == {"setting": {"wanted": "new", "live": "<absent>"}}
    assert output["verifyAttempts"] == 1 and verify_sleeps == []
    assert posts == 1


@pytest.mark.parametrize("page", ["wconfig", "dhcpserver", "etherlan"])
def test_unreadable_verification_exits_2_on_wifi_and_lan_paths(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps, page
):
    token = page.upper()
    code, output, posts, _ = _run(monkeypatch, capsys, [PLEASE_WAIT] * 3, page=page, token=token)
    assert code == 2 and output["committed"] is True and output.get("verified") is None
    assert "<absent>" not in json.dumps(output)
    assert posts == 1
