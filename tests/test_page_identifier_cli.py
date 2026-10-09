"""Page ids at the CLI boundary: an unknown but well-formed id is a page failure (exit 2); a malformed
id (hyphens) is a usage error (exit 1). Guards the ids used by scripts/e2e.py."""

from __future__ import annotations

import json

from save_helpers import client_with, html

from bgwcli import cli


def test_unknown_well_formed_page_id_is_page_unavailable_exit_2(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    client, wire = client_with(lambda request, n: html("<html><title>Page not found</title></html>", status=404))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["page", "nosuchpagexyz"])
    out = capsys.readouterr().out
    assert code == 2
    assert "Page unavailable" in out
    assert any(r.url.endswith("/nosuchpagexyz.ha") for r in wire.requests)


def test_hyphenated_page_id_is_a_usage_error_exit_1(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    client, wire = client_with(lambda request, n: (_ for _ in ()).throw(AssertionError("router must not be contacted")))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["page", "no-such-page-xyz", "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert "Invalid CGI page identifier" in json.loads(captured.out)["error"]
    assert wire.requests == []
