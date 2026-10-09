"""A form page answered without any form controls (a "Please wait" document) is a failed read."""

import json
from urllib.parse import urlsplit

from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, form, html

from bgwcli import cli
from bgwcli.dumpfile import read_dump_file, write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


def _dump_with(tmp_env, page, name, value):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={page: {name: value}}))
    return str(path)


def test_dump_refuses_a_form_page_without_controls(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if urlsplit(request.url).path.endswith("wconfig.ha"):
            return html(PLEASE_WAIT)
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)

    path = tmp_env / "backup.json"
    code, out, posts = _run(monkeypatch, capsys, handler, ["dump", "--out", str(path)])
    assert code == 2 and posts == []
    assert not path.exists()
    assert [f["page"] for f in out["failures"]] == ["wconfig"]


def test_diff_exits_2_when_a_form_page_has_no_controls(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(
        monkeypatch, capsys, lambda r, n: html(PLEASE_WAIT),
        ["diff", _dump_with(tmp_env, "wconfig", "ssidname11", "RecoveredWiFi"), "--include", "wconfig"],
    )
    assert code == 2 and posts == []


def test_restore_does_not_treat_a_blank_form_page_as_removed_fields(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _run(
        monkeypatch, capsys, lambda r, n: html(PLEASE_WAIT),
        ["restore", _dump_with(tmp_env, "wconfig", "ssidname11", "RecoveredWiFi"), "--include", "wconfig",
         "--commit", "--confirm", "RESTORE"],
    )
    assert code == 2 and posts == []
    assert out["ok"] is False and out["failures"][0]["page"] == "wconfig"


def test_a_page_with_controls_still_tolerates_individually_removed_fields(tmp_env, clock, monkeypatch, capsys):
    state = {"posted": False}

    def handler(request, n):
        if request.method == "POST":
            state["posted"] = True
            return html("", 302, {"location": "/cgi-bin/dosprotect.ha"})
        return html(form("dosprotect", "new" if state["posted"] else "old"))

    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"),
                                   forms={"dosprotect": {"setting": "new", "gone": "x"}}))
    code, out, posts = _run(
        monkeypatch, capsys, handler,
        ["restore", str(path), "--include", "dosprotect", "--commit", "--confirm", "RESTORE"],
    )
    assert len(posts) == 1
    assert read_dump_file(path).forms["dosprotect"]["gone"] == "x"
