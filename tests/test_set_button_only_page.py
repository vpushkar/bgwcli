"""set needs at least one settable control on the page: a button-only page is refused before any POST."""

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli

BUTTON_ONLY = (
    '<form action="/cgi-bin/etherlan.ha"><input type="hidden" name="nonce" value="abc123">'
    '<input type="submit" name="Reboot" value="Reboot"></form>'
)


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, captured, posts


@pytest.mark.parametrize("commit", [True, False])
def test_set_on_a_button_only_page_is_a_usage_error_without_a_post(tmp_env, clock, monkeypatch, capsys, commit):
    argv = ["set", "etherlan", "setting=new"]
    if commit:
        argv += ["--commit", "--confirm", "ETHERLAN"]
    code, captured, posts = _run(monkeypatch, capsys, lambda r, n: html(BUTTON_ONLY), argv)
    out = json.loads(captured.out)
    assert code == 1 and posts == []
    assert out["errorType"] == "UsageError"
    assert "etherlan" in out["error"] and "no settable fields" in out["error"]
    assert "submit" in out["error"] and "action" in out["error"]


def test_set_on_a_page_with_a_settable_field_is_unchanged(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/etherlan.ha"})
        return html(form("etherlan", "old"))

    code, captured, posts = _run(monkeypatch, capsys, handler,
                                 ["set", "etherlan", "setting=new"])
    assert code == 0 and posts == []
    assert json.loads(captured.out)["dryRun"] is True
