"""autorestore's three-run limit counts only runs that sent (or may have sent) a configuration write."""

from __future__ import annotations

import json

from save_helpers import client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.dumpfile import write_dump_file
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"


def _run_sequence(tmp_env, monkeypatch, capsys, modes):
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / "baseline.json"
    write_dump_file(path, dump)
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    observations = []
    for mode in modes:
        state = {"gets": 0}

        def handler(request, number, state=state, mode=mode):
            if request.method == "POST":
                if mode == "noack":
                    raise TimeoutError("lost")
                return html(form(PAGE, "old"))
            state["gets"] += 1
            if mode == "presend" and state["gets"] == 2:
                raise TimeoutError("nonce read timed out")
            return html(form(PAGE, "old"))

        client, wire = client_with(handler)
        monkeypatch.setattr(cli, "_client_factory", lambda *a, client=client, **kw: client)
        code = cli.main([
            "autorestore", str(path), "--include", PAGE, "--host", "router.local",
            "--commit", "--confirm", "RESTORE", "--max-passes", "1", "--json",
        ])
        out = json.loads(capsys.readouterr().out)
        checkpoint = RecoveryCheckpoint("router.local", dump, (PAGE,))
        record = json.loads(checkpoint.path.read_text()) if checkpoint.path.exists() else None
        observations.append({
            "mode": mode, "exit": code, "status": out["status"], "reason": out["reason"], "record": record,
            "posts": sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests),
        })
    return observations


def test_pre_send_faults_neither_count_nor_reset_and_never_stop_recovery(tmp_env, monkeypatch, capsys, clock):
    runs = _run_sequence(tmp_env, monkeypatch, capsys, ["noack", "presend", "presend", "presend", "presend"])
    assert runs[0]["posts"] == 1 and runs[0]["record"]["failures"]["count"] == 1
    for run in runs[1:]:
        assert run["posts"] == 0
        assert run["record"] is not None, "the recovery intent stays for the next run"
        assert run["record"]["failures"]["count"] == 1, "a pre-send fault neither increments nor resets"
        assert run["status"] == "router-unreachable" and "consecutive runs" not in run["reason"]


def test_a_run_of_only_pre_send_faults_leaves_no_intent_and_no_count(tmp_env, monkeypatch, capsys, clock):
    runs = _run_sequence(tmp_env, monkeypatch, capsys, ["presend"] * 5)
    for run in runs:
        assert run["posts"] == 0
        assert run["record"] is None
        assert "consecutive runs" not in run["reason"]
