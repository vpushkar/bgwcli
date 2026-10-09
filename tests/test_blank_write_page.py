"""set / submit / action on any page that answers without a form control is a failed read (exit 2)
before any POST, not only the dump pages."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
MAC = "aa:bb:cc:dd:ee:ff"

CASES = [
    (["set", "wconfig_unified", "ssidname11=x", "--confirm", "WCONFIG-UNIFIED"], "wconfig_unified"),
    (["set", "ipalloc", f"alloc_{MAC}=192.168.1.5", "--confirm", "IPALLOC"], "ipalloc"),
    (["action", "clear-lan-statistics", "--confirm", "CLEAR-LAN-STATISTICS"], "lanstatistics"),
    (["submit", "ipalloc", f"Allocate_{MAC}", "--confirm", "IPALLOC"], "ipalloc"),
]


@pytest.mark.parametrize(("argv", "page"), CASES)
def test_blank_page_is_a_failed_read_before_any_post(tmp_env, clock, monkeypatch, capsys, argv, page):
    client, wire = client_with(lambda r, n: html(PLEASE_WAIT))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert posts == []
    assert code == 2
    assert out["ok"] is False
    assert out["page"] == page and "form controls" in out["error"]


def test_a_button_only_form_page_is_still_a_form(tmp_env, clock, monkeypatch, capsys):
    page = ('<form action="/cgi-bin/lanstatistics.ha"><input type="submit" name="Clear" '
            'value="Clear Statistics"></form>')
    client, wire = client_with(lambda r, n: html(page))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["submit", "lanstatistics", "Clear", "--json"])
    json.loads(capsys.readouterr().out)
    assert code == 0
    assert [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")] == []
