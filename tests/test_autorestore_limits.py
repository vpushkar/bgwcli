"""autorestore's three-run limit and write-unanswered evidence on every path that sent a write."""

from __future__ import annotations

import json

import pytest
from save_helpers import NO_CHANGE, client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import Snapshot, SnapshotMeta


def _runs(tmp_env, monkeypatch, capsys, case, runs=4):
    page = "wconfig" if case == "nochange" else "dosprotect"
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={page: {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    sleeps: list[float] = []
    monkeypatch.setattr(autorestore, "_sleep", sleeps.append)
    observations = []
    for _ in range(runs):
        state = {"posted": False}

        def handler(request, number, state=state):
            if request.method == "POST":
                state["posted"] = True
                if case == "closing-fault":
                    raise TimeoutError("synthetic lost configuration response")
                if case == "nochange":
                    return html(form(page, "old", banner=NO_CHANGE))
                return html(form(page, "old"))
            if state["posted"] and case == "closing-fault":
                return html("<title>Unavailable</title>", 503)
            return html(form(page, "old"))

        client, wire = client_with(handler)
        monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
        code = cli.main([
            "autorestore", str(path), "--include", page, "--host", "router.local",
            "--commit", "--confirm", "RESTORE", "--max-passes", "1", "--json",
        ])
        out = json.loads(capsys.readouterr().out)
        checkpoint = RecoveryCheckpoint("router.local", dump, (page,))
        record = json.loads(checkpoint.path.read_text()) if checkpoint.path.exists() else None
        observations.append({
            "exit": code, "status": out["status"], "writeUnanswered": out["writeUnanswered"], "reason": out["reason"],
            "posts": sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests),
            "record": record,
        })
    return observations


@pytest.mark.parametrize("case", ["noack", "closing-fault"])
def test_unanswered_write_runs_count_and_keep_the_flag_on_the_limit_run(tmp_env, monkeypatch, capsys, clock, case):
    outcomes = _runs(tmp_env, monkeypatch, capsys, case)
    assert [r["posts"] for r in outcomes] == [1, 1, 1, 0]
    assert all(r["exit"] == 2 for r in outcomes)
    assert [r["writeUnanswered"] for r in outcomes[:3]] == [True, True, True]
    assert [r["record"]["failures"]["count"] for r in outcomes[:3]] == [1, 2, 3]
    assert "consecutive runs" in outcomes[2]["reason"] and "nothing sent" in outcomes[3]["reason"]


def test_no_change_mismatch_keeps_its_counter_and_stops_after_three_runs(tmp_env, monkeypatch, capsys, clock):
    outcomes = _runs(tmp_env, monkeypatch, capsys, "nochange")
    assert [r["posts"] for r in outcomes] == [1, 1, 1, 0]
    assert [r["exit"] for r in outcomes] == [1, 1, 2, 2]
    assert [r["record"]["failures"]["count"] for r in outcomes[:3]] == [1, 2, 3]
    assert all(r["writeUnanswered"] is False for r in outcomes)
