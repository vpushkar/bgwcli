"""A closed stdout (`bgwcli autorestore --commit | head -1`) never alters autorestore's control flow:
the run's result, exit code and recovery bookkeeping are those of the same run with an open stdout,
and no further writes are attempted once the pipe is known to be closed."""

from __future__ import annotations

import io
import json

import pytest
from save_helpers import SAVED_RED, client_with, form, html

from bgwcli import autorestore, cli
from bgwcli import format as fmt
from bgwcli.dumpfile import write_dump_file
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.restore import RestoreStepResult
from bgwcli.snapshot import Snapshot, SnapshotMeta

PAGE = "dosprotect"


class ClosedPipe(io.StringIO):
    """A stdout whose reader went away after `good` successful calls of `fail_on` (write or flush)."""

    def __init__(self, fail_on: str, good: int = 1):
        super().__init__()
        self.fail_on, self.good = fail_on, good
        self.calls = {"write": 0, "flush": 0}
        self.failed = False
        self.calls_after_failure = 0

    def _call(self, name):
        if self.failed:
            self.calls_after_failure += 1
        self.calls[name] += 1
        if name == self.fail_on and self.calls[name] > self.good:
            self.failed = True
            raise BrokenPipeError(32, "Broken pipe")

    def write(self, text):
        self._call("write")
        return super().write(text)

    def flush(self):
        self._call("flush")
        super().flush()


def _scenario(tmp_env, monkeypatch, label, stdout=None):
    """One committed run whose write POST gets no answer (exit 2, failure counted, intent kept)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_env / f"state-{label}"))
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_env / f"cache-{label}"))
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / f"baseline-{label}.json"
    write_dump_file(path, dump)
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)

    def handler(request, _number):
        if request.method == "POST":
            raise TimeoutError("lost")
        return html(form(PAGE, "old"))

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    if stdout is not None:
        monkeypatch.setattr("sys.stdout", stdout)
    code = cli.main([
        "autorestore", str(path), "--include", PAGE, "--host", "router.local",
        "--commit", "--confirm", "RESTORE", "--max-passes", "1",
    ])
    checkpoint = RecoveryCheckpoint("router.local", dump, (PAGE,))
    record = json.loads(checkpoint.path.read_text()) if checkpoint.path.exists() else None
    posts = sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests)
    return code, record, posts


@pytest.mark.parametrize("fail_on", ["write", "flush"])
def test_closed_stdout_leaves_result_and_bookkeeping_unchanged(clock, tmp_env, monkeypatch, capsys, fail_on):
    normal = io.StringIO()
    baseline = _scenario(tmp_env, monkeypatch, "open", normal)
    assert baseline[0] == 2 and baseline[1]["failures"]["count"] == 1 and baseline[2] == 1
    assert normal.getvalue().count("\n") >= 3, "the run prints several lines"

    pipe = ClosedPipe(fail_on)
    broken = _scenario(tmp_env, monkeypatch, "closed", pipe)
    assert pipe.failed, "the pipe closed during the run"
    assert broken == baseline
    assert pipe.calls_after_failure == 0, "nothing is written after the first BrokenPipeError"


def test_a_closed_stdout_in_json_mode_still_reports_the_run_exit_code(clock, tmp_env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_env / "state-json"))
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_env / "cache-json"))
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / "baseline-json.json"
    write_dump_file(path, dump)
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    client, _wire = client_with(
        lambda request, _n: (_ for _ in ()).throw(TimeoutError("lost")) if request.method == "POST"
        else html(form(PAGE, "old"))
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    pipe = ClosedPipe("write", good=0)
    monkeypatch.setattr("sys.stdout", pipe)
    code = cli.main([
        "autorestore", str(path), "--include", PAGE, "--host", "router.local",
        "--commit", "--confirm", "RESTORE", "--max-passes", "1", "--json",
    ])
    assert code == 2 and pipe.failed and pipe.calls_after_failure == 0


class LateClosePipe(io.StringIO):
    """A stdout whose writes all succeed and whose flush fails only once the final output was written."""

    def __init__(self):
        super().__init__()
        self.armed = False
        self.failed = False

    def flush(self):
        if self.armed:
            self.failed = True
            raise BrokenPipeError(32, "Broken pipe")
        super().flush()


def _late_close_run(tmp_env, monkeypatch, label, json_mode, stdout):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_env / f"state-{label}"))
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_env / f"cache-{label}"))
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / f"baseline-{label}.json"
    write_dump_file(path, dump)
    monkeypatch.setattr(autorestore, "_sleep", lambda seconds: None)
    client, _wire = client_with(
        lambda request, _n: (_ for _ in ()).throw(TimeoutError("lost")) if request.method == "POST"
        else html(form(PAGE, "old"))
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    real_output = cli.Command.output

    def output(self, value, table_printer):
        real_output(self, value, table_printer)
        if isinstance(stdout, LateClosePipe):
            stdout.armed = True  # everything after the final write is the interpreter's exit flush

    monkeypatch.setattr(cli.Command, "output", output)
    monkeypatch.setattr("sys.stdout", stdout)
    argv = [
        "autorestore", str(path), "--include", PAGE, "--host", "router.local",
        "--commit", "--confirm", "RESTORE", "--max-passes", "1",
    ]
    return cli.main(argv + (["--json"] if json_mode else []))


@pytest.mark.parametrize("json_mode", [False, True])
def test_a_pipe_closed_after_the_last_progress_line_keeps_the_run_exit_code(clock, tmp_env, monkeypatch, json_mode):
    baseline = _late_close_run(tmp_env, monkeypatch, "open", json_mode, io.StringIO())
    assert baseline == 2
    pipe = LateClosePipe()
    code = _late_close_run(tmp_env, monkeypatch, "late", json_mode, pipe)
    assert pipe.failed, "the final output is flushed inside the guard, where the closed pipe is caught"
    assert code == baseline


def _restore_scenario(tmp_env, monkeypatch, label, stdout, json_mode=False):
    """One committed `restore` whose single form save is acknowledged and read back (exit 0)."""
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_env / f"cache-{label}"))
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={PAGE: {"setting": "new"}})
    path = tmp_env / f"baseline-{label}.json"
    write_dump_file(path, dump)
    posted = []

    def handler(request, _number):
        if request.method == "POST":
            posted.append(request)
            return html("", 302, {"location": f"/cgi-bin/{PAGE}.ha"})
        return html(form(PAGE, "new", banner=SAVED_RED) if posted else form(PAGE, "old"))

    client, _wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr("sys.stdout", stdout)
    argv = ["restore", str(path), "--include", PAGE, "--host", "router.local", "--commit", "--confirm", "RESTORE"]
    return cli.main(argv + (["--json"] if json_mode else []))


@pytest.mark.parametrize("json_mode", [False, True])
def test_a_closed_stdout_leaves_the_restore_exit_code_unchanged(clock, tmp_env, monkeypatch, json_mode):
    baseline = _restore_scenario(tmp_env, monkeypatch, "open", io.StringIO(), json_mode)
    assert baseline == 0
    pipe = ClosedPipe("write", good=0)
    code = _restore_scenario(tmp_env, monkeypatch, "closed", pipe, json_mode)
    assert pipe.failed, "the pipe closed during the run"
    assert code == baseline
    assert pipe.calls_after_failure == 0, "nothing is written after the first BrokenPipeError"


def test_print_restore_step_result_survives_a_closed_stream():
    step = RestoreStepResult(1, PAGE, "form", "save", "applied")
    for fail_on in ("write", "flush"):
        pipe = ClosedPipe(fail_on, good=0)
        assert fmt.print_restore_step_result(step, pipe) is False  # told the caller to stop
    open_stream = io.StringIO()
    assert fmt.print_restore_step_result(step, open_stream) is True
    assert "applied" in open_stream.getvalue()
