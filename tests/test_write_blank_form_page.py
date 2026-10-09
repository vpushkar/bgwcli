"""set, submit and form-button actions refuse a form page answered without controls, before any POST."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"


@pytest.mark.parametrize(
    "argv",
    [
        ["set", "wconfig", "ssidname11=x"],
        ["set", "etherlan", "a=b"],
        ["submit", "wconfig", "Save", "ssidname11=x"],
        ["action", "find-best-channel-5"],
    ],
)
def test_write_on_a_blank_form_page_is_a_failed_read(monkeypatch, tmp_env, clock, capsys, argv):
    client, wire = client_with(lambda r, n: html(PLEASE_WAIT))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    token = "CHANSCAN" if argv[0] == "action" else argv[1].upper()
    code = cli.main([*argv, "--commit", "--confirm", token, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert posts == []
    assert code == 2
    assert out["ok"] is False
    assert out.get("committed") in (None, False)
