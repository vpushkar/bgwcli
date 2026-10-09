"""Default (non --json, non --raw) stdout never carries router-derived escape sequences, control
characters or bidi overrides: preflight reasons, rescan and autorestore log lines, verification
messages and fixture-capture progress are sanitised where they are printed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from save_helpers import client_with, html
from test_restore_preflight_integration import Fetcher, Router, baseline, pages

from bgwcli import audit, cli
from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
from bgwcli.autorestore import AutorestoreResult
from bgwcli.config import GlobalOptions
from bgwcli.errors import RouterConnectionError, UsageError
from bgwcli.sweep import SweepPage
from bgwcli.terminal import is_terminal_hazard

HOSTILE = "\x1b[31mred\x1b[0m \x1b]0;pwned\x07 ‮evil\r\nnext\x00"


def hazards(text: str) -> list[str]:
    return [ch for ch in text if ch not in "\n\t" and is_terminal_hazard(ch)]


def run_text_restore(monkeypatch, client, fetcher, *, commit):
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", fetcher)
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())
    command = cli.Command(
        "restore",
        ["synthetic-dump.json"],
        GlobalOptions(json=False),
        commit=commit,
        confirm="RESTORE" if commit else None,
    )
    cli._run_restore(client, command)
    return command


@pytest.fixture
def hostile_preflight(monkeypatch):
    def inspect(client, requests, **_kwargs):
        raise UsageError(f"gateway said {HOSTILE}")

    monkeypatch.setattr(cli, "inspect_allocation_conflicts", inspect)


@pytest.mark.parametrize("commit", [False, True], ids=["dry-run", "commit"])
def test_a_preflight_failure_reason_is_sanitised(hostile_preflight, monkeypatch, capsys, commit):
    run_text_restore(monkeypatch, Router(), Fetcher(pages()), commit=commit)
    out = capsys.readouterr().out
    assert "gateway said" in out
    assert hazards(out) == []


def test_a_conflict_holder_and_reason_are_sanitised(monkeypatch, capsys):
    conflict = AllocationConflict(f"192.168.1.67{HOSTILE}", "02:0a:0b:0c:0d:04", f"aa{HOSTILE}", "name", "off")
    monkeypatch.setattr(
        cli, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight([conflict], {"Clear": "Clear"})
    )
    run_text_restore(monkeypatch, Router(), Fetcher(pages()), commit=False)
    out = capsys.readouterr().out
    assert "allocation conflict" in out
    assert hazards(out) == []


def test_rescan_log_lines_are_sanitised(monkeypatch, capsys):
    conflict = AllocationConflict("192.168.1.67", "02:0a:0b:0c:0d:04", "02:0a:0b:0c:0d:09", "name", "off")
    monkeypatch.setattr(
        cli, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight([conflict], {"Clear": "Clear"})
    )

    def rescan(_client, _preflight, *, log=lambda _line: None, evidence=None):
        log(f"Rescan: {HOSTILE}")
        raise UsageError("stop here")

    monkeypatch.setattr(cli, "rescan_allocation_conflicts", rescan)
    run_text_restore(monkeypatch, Router(), Fetcher(pages()), commit=True)
    out = capsys.readouterr().out
    assert "Rescan: red " in out
    assert hazards(out) == []


def test_a_post_restore_verification_message_is_sanitised(hostile_preflight, monkeypatch, capsys):
    monkeypatch.setattr(cli, "inspect_allocation_conflicts", lambda *_a, **_k: AllocationPreflight([], None))

    class Failing(Fetcher):
        def __call__(self, client, page_ids):
            if self.calls:
                raise RouterConnectionError(f"re-read failed {HOSTILE}")
            return super().__call__(client, page_ids)

    run_text_restore(monkeypatch, Router(holder_mac="02:0a:0b:0c:0d:04"), Failing(pages()), commit=True)
    out = capsys.readouterr().out
    assert "Post-restore verification unavailable: re-read failed" in out
    assert hazards(out) == []


def test_autorestore_log_lines_are_sanitised(tmp_env, monkeypatch, capsys):
    path = tmp_env / "dump.json"
    path.write_text("{}")
    monkeypatch.setattr(cli, "read_dump_file", lambda _path: baseline())

    def fake_run(*_args, log, **_kwargs):
        log(f"error: {HOSTILE}")
        return AutorestoreResult("error", f"reason {HOSTILE}")

    monkeypatch.setattr(cli, "run_autorestore", fake_run)
    client, _wire = client_with(lambda request, number: html("<html></html>"))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    cli.main(["autorestore", str(path), "--host", "router.local"])
    out = capsys.readouterr().out
    assert "error: red " in out
    assert hazards(out) == []


def test_fixture_capture_progress_is_sanitised(tmp_path):
    failed = SweepPage("S", "L", "devices", False, False, False, error=f"timed out {HOSTILE}")
    rendered = Path(tmp_path) / "out.txt"
    with rendered.open("w+") as stream:
        audit.capture_fixture_pack([failed], tmp_path / "fixtures", stdout=stream)
        stream.seek(0)
        text = stream.read()
    assert "failed: timed out red " in text
    assert hazards(text) == []
    assert json.loads((tmp_path / "fixtures" / "expected" / "devices.json").read_text())["error"]
