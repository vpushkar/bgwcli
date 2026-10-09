"""A nonce page refused before any POST is a structural failure on every save path: Wi-Fi and LAN
steps (which run through the restore step runner and carry `writePerformed: false` as truthful
evidence) must never be read as the gateway's "No changes detected" answer."""

from __future__ import annotations

import json

import pytest
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import cli

WAIT = "<html><head><title>Please wait</title></head><body>Please wait...</body></html>"
NO_CHANGE = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">No changes detected. Save not performed.</div>'
)
ROWS = [
    ("submit", "wconfig", ["Save"], None, "WCONFIG"),
    ("set", "wconfig", ["maxclients=79"], "79", "WCONFIG"),
    ("set", "wconfig", ["maxclients=80"], "80", "WCONFIG"),
    ("set", "dhcpserver", ["dhcp=off"], "off", "DHCPSERVER"),
]
IDS = ["submit-wconfig-save", "set-wconfig-79", "set-wconfig-80", "set-dhcpserver-off"]


def _page(page, live):
    field = "dhcp" if page == "dhcpserver" else "maxclients"
    return form(page, live, field)


def _run(monkeypatch, capsys, operation, page, args, token, *, reads_before_wait=1, live="80"):
    """Valid reads up to `reads_before_wait`, then the Please wait page for every later GET."""
    gets = []

    def handle(request, n):
        if request.method == "POST":
            raise AssertionError("a refused nonce page must not be followed by a POST")
        gets.append(request.url)
        if len(gets) <= reads_before_wait:
            return html(_page(page, live))
        return html(WAIT)

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([operation, page, *args, "--commit", "--confirm", token, "--json"])
    output = json.loads(capsys.readouterr().out)
    return code, output, wire, gets


@pytest.mark.parametrize(("operation", "page", "args", "_value", "token"), ROWS, ids=IDS)
def test_presend_nonce_refusal_is_structural_not_no_change(
    clock, tmp_env, capsys, monkeypatch, operation, page, args, _value, token
):
    live = "on" if page == "dhcpserver" else "80"
    code, output, wire, gets = _run(monkeypatch, capsys, operation, page, args, token, live=live)
    text = json.dumps(output)
    assert code == 2
    assert output["outcome"] == "failed" and output["committed"] is False
    assert output["writeAttempted"] is False and output["writePerformed"] is False
    assert "No changes detected" not in text and "sent once" not in text
    assert "write nonce" in text
    assert not any(r.method == "POST" for r in wire.requests)
    assert not clock.sleeps
    # No verification re-read: every GET up to and including the refused one, and none after.
    assert gets[-1] == wire.requests[-1].url
    assert output.get("verified") is None and output.get("verifyAttempts") is None


@pytest.mark.parametrize("operation", ["set", "submit"])
def test_real_wifi_no_change_still_unchanged(clock, tmp_env, capsys, monkeypatch, operation):
    client, wire = client_with(lambda request, n: html(
        _page("wconfig", "80") if n <= 2 else form("wconfig", "80", "maxclients", NO_CHANGE)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    args = ["set", "wconfig", "maxclients=80"] if operation == "set" else ["submit", "wconfig", "Save"]
    code = cli.main([*args, "--commit", "--confirm", "WCONFIG", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["outcome"] == "unchanged"
    assert output["writeAttempted"] is True and output["writePerformed"] is False
    if operation == "set":
        assert output["verified"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_lan_no_change_changes_saved_still_applied(clock, tmp_env, capsys, monkeypatch):
    client, wire = client_with(lambda request, n: html(
        form("dhcpserver", "on", "dhcp") if n <= 2 else form("dhcpserver", "off", "dhcp", SAVED_RED)
    ))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", "dhcpserver", "dhcp=off", "--commit", "--confirm", "DHCPSERVER", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["outcome"] == "applied" and output["committed"] is True
    assert sum(r.method == "POST" for r in wire.requests) == 1


def test_decision_never_reads_a_failed_unsent_step_as_no_change():
    from bgwcli.save_result import decide_save_result

    refused = decide_save_result("failed", "nonce page refused; nothing was sent", False, None,
                                 requested=True, write_attempted=False)
    assert (refused.exit_code, refused.outcome, refused.committed) == (2, "failed", False)
    assert "No changes detected" not in refused.message
    sent = decide_save_result("unchanged", None, False, True, requested=True, write_attempted=True)
    assert (sent.exit_code, sent.outcome) == (0, "unchanged")


def test_generic_page_nonce_refusal_still_exit_two(clock, tmp_env, capsys, monkeypatch):
    code, output, wire, _gets = _run(monkeypatch, capsys, "set", "dosprotect", ["setting=new"], "DOSPROTECT")
    assert code == 2 and output.get("committed") is not True
    assert "No changes detected" not in json.dumps(output)
    assert not any(r.method == "POST" for r in wire.requests)
