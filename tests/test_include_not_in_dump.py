"""diff and restore --include naming only pages the dump never captured compare nothing: exit 1."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "{dump}", "--include", "etherlan"],
        ["restore", "{dump}", "--include", "etherlan", "--commit", "--confirm", "RESTORE"],
        ["autorestore", "{dump}", "--include", "etherlan"],
        ["autorestore", "{dump}", "--include", "etherlan", "--commit", "--confirm", "RESTORE"],
    ],
)
def test_include_of_pages_absent_from_the_dump_is_a_usage_error(tmp_env, clock, monkeypatch, capsys, argv):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"a": "b"}}))
    client, wire = client_with(lambda r, n: html("x"))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*[a.format(dump=path) for a in argv], "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 1
    assert out["ok"] is False and out["missingPages"] == ["etherlan"]
    assert "nothing compared: etherlan not in dump" in out["error"]
    assert wire.requests == []


def test_autorestore_nothing_selected_json_carries_the_status(tmp_env, clock, monkeypatch, capsys):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"a": "b"}}))
    client, wire = client_with(lambda r, n: html("x"))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["autorestore", str(path), "--include", "etherlan", "--json"])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert code == 1
    assert out["status"] == "nothing-selected" and out["missingPages"] == ["etherlan"]
    assert "nothing compared: etherlan not in dump" in out["error"]
    assert wire.requests == []


def test_include_with_one_captured_page_still_compares(tmp_env, clock, monkeypatch, capsys):
    from save_helpers import form

    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "old"}}))
    client, wire = client_with(lambda r, n: html(form("dosprotect", "old")))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["diff", str(path), "--include", "dosprotect,etherlan", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["missingPages"] == ["etherlan"]
