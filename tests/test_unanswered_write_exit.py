"""A configuration write the gateway never answered (transport loss, HTTP error, refused session,
full session pool, no acknowledgement) keeps restore and autorestore at exit 2 even when the closing
re-read is readable: a later diff cannot turn "the write's fate is unknown" into "the router differs"."""

from __future__ import annotations

import json

import pytest
from integration_html import EMPTY_SECTION_TABLES
from save_helpers import ERROR, client_with, form, html

from bgwcli import autorestore, cli
from bgwcli.client import session_pool_full_error
from bgwcli.dumpfile import write_dump_file
from bgwcli.restore import RestoreStepResult
from bgwcli.snapshot import Snapshot, SnapshotMeta
from bgwcli.write_outcome import unanswered_write


def _dump(tmp_env):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}}))
    return path


def _handler(failure):
    def handler(request, _n):
        if request.method == "POST":
            if failure == "transport":
                raise TimeoutError("synthetic lost save response")
            if failure == "pool":
                raise session_pool_full_error(waited_ms=2300, retry_count=4)
            if failure == "auth":
                return html('<title>Login</title><input name="password">', 403)
            if failure == "rejected":
                return html(ERROR + form("dosprotect", "old"))
            if failure == "noack":
                # Answered, but "Changes saved" never appears on a readable page.
                return html(form("dosprotect", "old"))
            return html("<title>Unavailable</title>", 503)
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)
    return handler


def _main(monkeypatch, capsys, failure, argv):
    client, wire = client_with(_handler(failure))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main(argv)
    # Configuration POSTs only: a refused session may log in again (login.ha), never re-send the write.
    return code, json.loads(capsys.readouterr().out), sum(
        r.method == "POST" and "/login.ha" not in r.url for r in wire.requests
    )


@pytest.mark.parametrize("failure", ["http", "transport", "pool", "auth", "noack"])
def test_restore_with_an_unanswered_write_exits_2_despite_a_readable_closing_diff(
    tmp_env, clock, monkeypatch, capsys, failure
):
    code, out, posts = _main(monkeypatch, capsys, failure, [
        "restore", str(_dump(tmp_env)), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json",
    ])
    assert posts == 1
    assert out["execution"]["steps"][0]["status"] == "failed"
    if failure in ("pool", "auth"):
        # A full pool or a lost session ends the run: no closing read competes for the gateway.
        assert out["diff"] is None and out["verificationError"]["type"].startswith("Router")
    else:
        assert out["diff"] is not None and out["diff"]["identical"] is False
    assert code == 2 and out["writeUnanswered"] is True and out["unansweredWrite"]["page"] == "dosprotect"


def test_restore_with_an_explicit_rejection_stays_exit_1(tmp_env, clock, monkeypatch, capsys):
    code, out, posts = _main(monkeypatch, capsys, "rejected", [
        "restore", str(_dump(tmp_env)), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json",
    ])
    assert (code, posts) == (1, 1) and out["writeUnanswered"] is False and "unansweredWrite" not in out


@pytest.mark.parametrize("failure", ["http", "transport", "noack"])
def test_autorestore_with_an_unanswered_write_is_an_error_exit_2(tmp_env, clock, monkeypatch, capsys, failure):
    monkeypatch.setattr(autorestore, "_sleep", lambda _s: None)
    code, out, posts = _main(monkeypatch, capsys, failure, [
        "autorestore", str(_dump(tmp_env)), "--include", "dosprotect", "--commit", "--confirm", "RESTORE", "--json",
    ])
    assert posts == 1
    assert code == 2 and out["status"] == "error" and out["exitCode"] == 2
    assert "dosprotect" in out["reason"] and "no answer" in out["reason"]
    assert out["writeUnanswered"] is True


def test_unanswered_write_classification():
    base = {"order": 1, "page": "p", "kind": "form", "description": "d"}
    assert unanswered_write(RestoreStepResult(**base, status="failed", error="timed out", write_attempted=True))
    assert unanswered_write(RestoreStepResult(**base, status="failed", error="lost", write_attempted=None))
    assert unanswered_write(RestoreStepResult(**base, status="failed", error="full", session_pool_full=True,
                                              write_attempted=True))
    # A pool that was full before any write was sent is no unanswered write.
    assert not unanswered_write(RestoreStepResult(**base, status="failed", error="full", session_pool_full=True,
                                                  write_attempted=False))
    # Never sent, an explicit rejection, and an explicit "No changes detected" are answers.
    assert not unanswered_write(RestoreStepResult(**base, status="failed", error="nonce", write_attempted=False))
    assert not unanswered_write(RestoreStepResult(
        **base, status="failed", error="router rejected the change: bad", write_attempted=True))
    assert not unanswered_write(RestoreStepResult(
        **base, status="failed", error="No changes detected.", write_attempted=True, write_performed=False))
    assert not unanswered_write(RestoreStepResult(
        **base, status="failed", error="Timed out", write_attempted=True, acknowledgement_observed=True))
    assert not unanswered_write(RestoreStepResult(**base, status="applied", write_attempted=True))


def test_restore_lan_move_whose_post_got_no_response_exits_2_and_is_unanswered(tmp_env, clock, monkeypatch, capsys):
    path = tmp_env / "dump.json"
    write_dump_file(
        path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dhcpserver": {"ipaddr": "10.0.0.1"}})
    )

    def handler(request, _n):
        if request.method == "POST":
            raise TimeoutError("synthetic lost save response")
        return html(form("dhcpserver", "192.168.1.254", name="ipaddr"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main(["restore", str(path), "--include", "dhcpserver", "--commit", "--confirm", "RESTORE", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests)
    assert posts == 1
    assert out["execution"]["steps"][0]["status"] == "reconnect-required"
    assert code == 2 and out["writeUnanswered"] is True
