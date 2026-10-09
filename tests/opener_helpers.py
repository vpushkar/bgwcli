"""Shared fixtures for the opener-button tests (not a test module: import from here, never from test_*.py)."""

from __future__ import annotations

import json

from save_helpers import client_with

from bgwcli import cli

MAC = "02:0a:0b:0c:0d:02"
PACKET_FILTER = (
    '<form action="/cgi-bin/packetfilter.ha"><input type="hidden" name="nonce" value="ab12">'
    "<input type=\"submit\" name=\"AddDropRule\" value=\"Add a 'Drop' Rule\"></form>"
)


def run_cli(monkeypatch, capsys, argv, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts
