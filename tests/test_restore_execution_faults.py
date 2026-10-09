"""restore --commit: an execution fault is no answer (exit 2) even when the closing diff reads, and a
terminal pool-full or session failure ends the run without another router read."""

import json

import pytest
from save_helpers import client_with, form, html

from bgwcli import cli, session
from bgwcli.client import session_pool_full_error
from bgwcli.dumpfile import write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta


def _dump(tmp_env, page="dosprotect", name="setting", value="new"):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={page: {name: value}}))
    return str(path)


def RESTORE_ARGV(tmp_env):  # noqa: N802 - a constant-like argv builder
    return ["restore", _dump(tmp_env), "--include", "dosprotect", "--commit", "--confirm", "RESTORE"]


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts, client


@pytest.mark.parametrize("failure", ["http", "transport"])
def test_presend_fault_is_exit_2_not_exit_1(tmp_env, clock, monkeypatch, capsys, failure):
    state = {"reads": 0}

    def handler(request, _n):
        assert request.method == "GET"
        state["reads"] += 1
        if state["reads"] == 2:  # the nonce read just before the Save POST
            if failure == "http":
                return html("<title>Unavailable</title>", 503)
            raise TimeoutError("nonce transport lost")
        return html(form("dosprotect", "old"))

    code, out, posts, _ = _run(monkeypatch, capsys, handler,
                               RESTORE_ARGV(tmp_env))
    assert code == 2 and posts == []
    step = out["execution"]["steps"][0]
    assert step["writeAttempted"] is False and step["status"] == "failed"
    assert out["writeUnanswered"] is False and out["executionFault"]["page"] == "dosprotect"
    assert out["diff"] is not None


def test_a_rejected_restore_step_still_exits_1(tmp_env, clock, monkeypatch, capsys):
    from save_helpers import ERROR

    def handler(request, _n):
        if request.method == "POST":
            return html(form("dosprotect", "old", banner=ERROR))
        return html(form("dosprotect", "old"))

    code, out, posts, _ = _run(monkeypatch, capsys, handler,
                               RESTORE_ARGV(tmp_env))
    assert code == 1 and len(posts) == 1 and "executionFault" not in out


def test_pool_full_during_restore_skips_the_closing_read_and_records_the_cooldown(
    tmp_env, clock, monkeypatch, capsys
):
    state = {"posted": False, "after_pool": 0}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            raise session_pool_full_error(waited_ms=2300, retry_count=4)
        if state["posted"]:
            state["after_pool"] += 1
        return html(form("dosprotect", "old"))

    code, out, posts, client = _run(monkeypatch, capsys, handler,
                                    RESTORE_ARGV(tmp_env))
    assert code == 2 and len(posts) == 1 and out["sessionPoolFull"] is True
    assert state["after_pool"] == 0
    assert out["diff"] is None and out["verificationError"]["type"] == "RouterSessionPoolFullError"
    assert session.read_session_state(client.session_identity()).pool_cooldown_until is not None
