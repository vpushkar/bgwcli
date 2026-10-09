"""autorestore's initial (snapshot) read classification: an authentication-class refusal is an error.

A page-level 401/403 on the snapshot read means the gateway refuses this client, which the operator must
be told (error, exit 2); only transport faults and HTTP answers meaning "the router is not serving"
(5xx) are the quiet router-unreachable exit 0. Every test counts the POSTs sent."""

from __future__ import annotations

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"


def _run(tmp_env, monkeypatch, capsys, answer):
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    monkeypatch.setattr(autorestore, "_sleep", lambda _s: None)

    def handler(request, number):
        if request.method == "POST":
            if "login.ha" in request.url:
                return html("", status=302, headers={"location": "/cgi-bin/home.ha"})
            raise AssertionError("a configuration POST was sent")
        if isinstance(answer, Exception):
            raise answer
        return html("<title>Answer</title>", status=answer) if answer != 200 else html(form(PAGE, "old"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
    code = cli.main([
        "autorestore", str(path), "--include", PAGE, "--host", "router.local", "--commit", "--confirm", "RESTORE",
        "--max-passes", "3", "--wait", "120", "--json",
    ])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and "login.ha" not in r.url]
    return code, out, posts


@pytest.mark.parametrize("status", [401, 403])
def test_a_page_level_401_or_403_on_the_initial_read_is_an_error(tmp_env, monkeypatch, capsys, clock, status):
    code, out, posts = _run(tmp_env, monkeypatch, capsys, status)
    assert code == 2 and out["status"] == "error"
    assert posts == [], "nothing was sent"
    assert f"HTTP {status}" in out["reason"] and PAGE in out["reason"], "the operator is told which page and why"


def test_a_5xx_on_the_initial_read_stays_router_unreachable(tmp_env, monkeypatch, capsys, clock):
    code, out, posts = _run(tmp_env, monkeypatch, capsys, 503)
    assert code == 0 and out["status"] == "router-unreachable" and posts == []


def test_a_refused_connection_on_the_initial_read_stays_router_unreachable(tmp_env, monkeypatch, capsys, clock):
    code, out, posts = _run(tmp_env, monkeypatch, capsys, ConnectionRefusedError("refused"))
    assert code == 0 and out["status"] == "router-unreachable" and posts == []
