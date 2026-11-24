"""Diagnostics actions show the results page the gateway redirects them to.

Live on fw 6.35.8 (2026-11-24): diagnostics-*-details answer 302 -> etherdiag.ha / authdiag.ha /
ipdiag.ha / dnsdiag.ha and run-full-diagnostics answers 302 -> diag.ha; those pages carry the
results. bgwcli read each page for a rejection banner and then printed nothing.
"""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import client_with, html

from bgwcli import cli

DIAG_FORM = (
    "<html><head><title>Diagnostics</title></head><body>"
    '<form method="post" action="/cgi-bin/diag.ha"><input type="hidden" name="nonce" value="abc123">'
    "<table>"
    '<tr><th scope="row">Ethernet</th><td class="respass">Pass</td>'
    '<td><input type="submit" name="EthDetails" value="Details"></td></tr>'
    '<tr><th scope="row">IP</th><td class="respass">Pass</td>'
    '<td><input type="submit" name="IPDetails" value="Details"></td></tr>'
    "</table>"
    '<input type="submit" name="RunFullDiagnostics" value="Run Full Diagnostics">'
    "</form></body></html>"
)
ETHERDIAG = (
    "<html><head><title>Ethernet Diagnostics</title></head><body><table>"
    '<tr><th scope="row">Ethernet Ports</th><td><span class="respass">Pass</span></td></tr>'
    '<tr><th scope="row">Forwarding / Bridging</th><td><span class="respass">Fail</span></td></tr>'
    "</table></body></html>"
)


def _run(monkeypatch, capsys, argv, redirect):
    def handler(request, n):
        path = urlsplit(request.url).path
        if request.method == "POST":
            return html("", 302, {"location": redirect})
        if path == "/cgi-bin/etherdiag.ha":
            return html(ETHERDIAG)
        return html(DIAG_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(argv)
    out = capsys.readouterr().out
    gets = [urlsplit(r.url).path for r in wire.requests if r.method == "GET"]
    return code, out, gets


def test_details_action_reports_the_details_page_in_json(monkeypatch, capsys, tmp_env, clock):
    code, out, gets = _run(
        monkeypatch, capsys, ["action", "diagnostics-ethernet-details", "--commit", "--json"], "/cgi-bin/etherdiag.ha"
    )
    data = json.loads(out)
    assert code == 0 and data["committed"] is True
    assert data["answer"]["page"] == "etherdiag"
    assert data["answer"]["values"] == {"Ethernet Ports": "Pass", "Forwarding / Bridging": "Fail"}
    assert gets.count("/cgi-bin/etherdiag.ha") == 1, "the answer page is read once, as before"


def test_details_action_prints_the_details_page(monkeypatch, capsys, tmp_env, clock):
    code, out, _ = _run(
        monkeypatch, capsys, ["action", "diagnostics-ethernet-details", "--commit"], "/cgi-bin/etherdiag.ha"
    )
    assert code == 0
    assert "Ethernet Diagnostics" in out and "Forwarding / Bridging" in out and "Fail" in out


def test_full_diagnostics_reports_the_result_table(monkeypatch, capsys, tmp_env, clock):
    argv = ["action", "run-full-diagnostics", "--commit", "--json"]
    code, out, _ = _run(monkeypatch, capsys, argv, "/cgi-bin/diag.ha")
    data = json.loads(out)
    assert code == 0 and data["answer"]["page"] == "diag"
    assert any(row.get("Pass") is None and "Pass" in row.values() for row in data["answer"]["tables"]), data["answer"]


@pytest.mark.parametrize("action", ["clear-lan-statistics", "restart-wifi-2.4"])
def test_other_actions_carry_no_answer(action):
    from bgwcli.actions import get_action

    assert get_action(action).shows_answer is False
