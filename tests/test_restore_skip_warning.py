"""A skipped restore step explains itself in the commit output (text and --json), not only in the dry run.

A dump without a text-field note cannot tell a text field holding the unchecked marker from an unchecked
box; when the live page renders the field as text the step is skipped with a warning naming the field."""

from __future__ import annotations

import io
import json

from save_helpers import SAVED_RED, client_with, html

from bgwcli import cli
from bgwcli import format as fmt
from bgwcli.dumpfile import write_dump_file
from bgwcli.restore import RestoreStep, RestoreStepResult, execute_restore
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"
FIELD = "flag"


def _live(other: str, banner: str = "") -> str:
    # `flag` renders as a text control, so the dump's unchecked marker cannot be applied to it.
    return (
        f'{banner}<form action="/cgi-bin/{PAGE}.ha"><input name="nonce" value="abc123">'
        f'<input type="text" name="{FIELD}" value="zq9">'
        f'<input type="text" name="other" value="{other}">'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def _router(tmp_env, monkeypatch, dump_name):
    """A dump with the unchecked marker on a text field, and a client whose router applies the Save."""
    dump = tmp_env / dump_name
    write_dump_file(
        dump,
        Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {FIELD: "<unchecked>", "other": "new"}}),
    )
    state = {"posted": False}

    def handle(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(_live("new", SAVED_RED))
        return html(_live("new" if state["posted"] else "old"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    return dump, wire


def _run(tmp_env, capsys, monkeypatch, extra):
    dump, wire = _router(tmp_env, monkeypatch, "dump.json")
    code = cli.main(["restore", str(dump), "--include", PAGE, *extra])
    captured = capsys.readouterr()
    posts = sum(r.method == "POST" and not r.url.split("?")[0].endswith("/login.ha") for r in wire.requests)
    return code, captured.out, posts


def test_commit_text_output_prints_the_skip_reason(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    code, out, posts = _run(tmp_env, capsys, monkeypatch, ["--commit", "--confirm", "RESTORE"])
    lines = out.splitlines()
    skipped = [i for i, line in enumerate(lines) if "skipped" in line and PAGE in line and line.startswith("[")]
    assert len(skipped) == 1
    warning = lines[skipped[0] + 1]
    assert warning.startswith("    warning: ") and FIELD in warning
    assert "<unchecked>" not in warning and "zq9" not in warning
    assert posts == 1  # only the applied form save; the skipped step posts nothing
    assert code == 1  # the closing diff still differs on the field left out of the save


def test_commit_json_carries_the_warning_on_the_skipped_step_only(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    code, out, posts = _run(tmp_env, capsys, monkeypatch, ["--commit", "--confirm", "RESTORE", "--json"])
    steps = json.loads(out)["execution"]["steps"]
    skipped = [s for s in steps if s["status"] == "skipped"]
    applied = [s for s in steps if s["status"] == "applied"]
    assert len(skipped) == 1 and len(applied) == 1
    assert FIELD in skipped[0]["warning"] and "<unchecked>" not in skipped[0]["warning"]
    assert "warning" not in applied[0]
    assert posts == 1 and code == 1


def test_dry_run_still_prints_the_warning_once(clock, tmp_env, capsys, monkeypatch):
    code, out, posts = _run(tmp_env, capsys, monkeypatch, [])
    assert out.count("    warning: ") == 1
    assert posts == 0 and code == 0


class _NoPost:
    def post_cgi_page(self, *args, **kwargs):  # pragma: no cover - a skipped step never posts
        raise AssertionError("a skipped step must not post")


def test_execute_restore_copies_the_skip_warning_and_leaves_other_steps_without_one():
    skip = RestoreStep(order=1, kind="skip", page=PAGE, description="left out", warning="field is left out")
    blocked = RestoreStep(order=2, kind="form", page=PAGE, description="d", blocked="not offered")
    results = execute_restore(_NoPost(), [skip, blocked]).steps
    assert results[0].status == "skipped" and results[0].warning == "field is left out"
    assert results[1].warning is None


def _warning_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("    warning: ") and FIELD in line]


def test_print_restore_step_result_prints_the_warning_under_a_skipped_step():
    result = RestoreStepResult(
        order=1, page=PAGE, kind="skip", description="left out", status="skipped", warning=f"{FIELD} is left out"
    )
    out = io.StringIO()
    assert fmt.print_restore_step_result(result, out) is True
    assert _warning_lines(out.getvalue()) == [f"    warning: {FIELD} is left out"]


def test_restore_commit_prints_the_skip_warning_once_and_not_the_plan(
    clock, tmp_env, capsys, monkeypatch, verify_sleeps
):
    _code, out, _posts = _run(tmp_env, capsys, monkeypatch, ["--commit", "--confirm", "RESTORE"])
    assert len(_warning_lines(out)) == 1
    assert not any(line.startswith("[") and " form: " in line for line in out.splitlines()), "no plan in commit mode"


def test_autorestore_commit_prints_the_skip_warning_once(clock, tmp_env, capsys, monkeypatch, verify_sleeps):
    from bgwcli import autorestore

    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    dump, _wire = _router(tmp_env, monkeypatch, "auto-dump.json")
    code = cli.main([
        "autorestore", str(dump), "--include", PAGE, "--on-any-diff", "--max-passes", "1",
        "--commit", "--confirm", "RESTORE",
    ])
    out = capsys.readouterr().out
    assert len(_warning_lines(out)) == 1, out
    assert any(line.startswith("[") and " skipped " in line for line in out.splitlines()), out
    assert code == 1  # the field left out of the save still differs after the pass
