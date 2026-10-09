"""A sitemap answer without any /cgi-bin/*.ha link is an unavailable page, never an empty sitemap."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>busy</body></html>"


@pytest.mark.parametrize("command", ["sitemap", "coverage"])
@pytest.mark.parametrize("body", [PLEASE_WAIT, "", "<html><body><a href='/other'>x</a></body></html>"])
def test_blank_sitemap_is_no_answer(tmp_env, clock, monkeypatch, capsys, command, body):
    client, wire = client_with(lambda r, n: html(body))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([command, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["ok"] is False and "sitemap" in out["error"]
    assert "liveCount" not in out
    assert not any(r.method == "POST" and not r.url.endswith("login.ha") for r in wire.requests)
