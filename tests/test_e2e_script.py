"""Guards for scripts/e2e.py helpers (the live run itself needs a gateway and is not exercised here)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e.py"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("bgw_e2e_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def test_page_ok_flags_a_page_parsed_to_zero_values_tables_fields_and_selects(e2e):
    empty = {"page": "x", "values": {}, "tables": [], "fields": [], "selects": [], "summary": {"Mode": "auto"}}
    assert e2e.page_ok("", empty) == "page parsed to zero values/tables/fields/selects"
    for key, value in (("values", {"A": "1"}), ("tables", [{"A": "1"}]), ("fields", [{}]), ("selects", [{}])):
        assert e2e.page_ok("", {**empty, key: value}) is None, key
    # composite/fallback payloads without those keys are judged by their own fields
    assert e2e.page_ok("", {"fallback": True, "sections": [{"ok": True}]}) is None
    assert e2e.page_ok("", {"ok": False, "error": "boom"}) == "page reported failure: boom"


def test_build_cases_writes_dumps_and_sweep_artifacts_under_out(e2e, tmp_path):
    out = tmp_path / "run"
    cases = e2e.build_cases(out)
    argv_paths = [arg for case in cases for arg in case.argv if "/" in arg and arg.startswith("/")]
    assert argv_paths, "expected dump/sweep paths in the case argv"
    # the output directory itself is the one bare path (the dump --out refusal case)
    assert all(arg == str(out) or arg.startswith(str(out) + "/") for arg in argv_paths), argv_paths
    assert not any("/tmp/e2e" in arg for case in cases for arg in case.argv)


def test_run_records_a_timed_out_case_as_failed_and_writes_private_output(e2e, tmp_path, monkeypatch):
    import os
    import stat
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"), output=b"partial out", stderr=None)

    monkeypatch.setattr(e2e.subprocess, "run", timeout)
    old_umask = os.umask(0o022)
    try:
        result = e2e.run(e2e.Case("slow", ["sweep"]), {}, tmp_path, 7)
    finally:
        os.umask(old_umask)
    assert result.problems and "timed out" in result.problems[0]
    assert result.rc != 0
    assert (tmp_path / "007-slow.out").read_text() == "partial out"
    assert (tmp_path / "007-slow.err").read_text() == ""
    for name in ("007-slow.out", "007-slow.err"):
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600


def test_run_writes_case_output_owner_only(e2e, tmp_path, monkeypatch):
    import os
    import stat
    import subprocess

    monkeypatch.setattr(
        e2e.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="ok\n", stderr="")
    )
    old_umask = os.umask(0o022)
    try:
        result = e2e.run(e2e.Case("fast", ["tabs"]), {}, tmp_path, 1)
    finally:
        os.umask(old_umask)
    assert result.problems == []
    for name in ("001-fast.out", "001-fast.err"):
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600


def test_write_private_refuses_existing_files_and_symlinks(e2e, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep")
    (tmp_path / "link.out").symlink_to(target)
    with pytest.raises(OSError):
        e2e.write_private(tmp_path / "link.out", "router data")
    assert target.read_text() == "keep"
    (tmp_path / "old.out").write_text("previous run")
    with pytest.raises(FileExistsError):
        e2e.write_private(tmp_path / "old.out", "router data")


def test_output_directory_defaults_to_a_fresh_private_temp_dir_and_refuses_a_used_one(e2e, tmp_path):
    import os
    import stat

    fresh = e2e.output_directory(None)
    try:
        assert fresh.is_dir() and not any(fresh.iterdir()) and str(fresh) != "/tmp/e2e"
        assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o700
    finally:
        fresh.rmdir()
    created = e2e.output_directory(str(tmp_path / "new"))
    assert created.is_dir() and stat.S_IMODE(os.stat(created).st_mode) == 0o700
    (created / "report.md").write_text("old")
    with pytest.raises(SystemExit):
        e2e.output_directory(str(created))


def test_docstring_lists_the_commits_the_run_performs(e2e):
    doc = e2e.__doc__
    for words in ("diagnostics ping", "nslookup", "traceroute", "run-speed-test", "dosprotect", "--skip-commits"):
        assert words in doc, words


def test_execute_cases_docstring_names_the_real_dosprotect_field_not_a_placeholder(e2e):
    doc = e2e.execute_cases.__doc__
    assert e2e.DOSPROTECT_FIELD in doc and "{DOSPROTECT_FIELD}" not in doc


def test_a_missing_cli_is_refused_before_any_output_directory_is_created(e2e, tmp_path, monkeypatch, capsys):
    out = tmp_path / "e2e-out"
    monkeypatch.setattr(e2e, "BGW", str(tmp_path / "no-venv" / "bin" / "bgwcli"))
    monkeypatch.setattr("sys.argv", ["e2e.py", "--out", str(out)])
    monkeypatch.setenv("E2E_ACCESS_CODE", "x")
    assert e2e.main() == 2
    assert "not found" in capsys.readouterr().err
    assert not out.exists()


def test_the_missing_include_case_expects_the_nothing_compared_contract(e2e, tmp_path):
    case = next(c for c in e2e.build_cases(tmp_path) if c.name == "restore-dry-include-missing")
    assert case.rc == 1 and case.stdout_empty is True
    assert "nothing compared" in case.stderr_has


def test_the_owning_form_refusal_case_is_a_read_only_dry_run_kept_by_skip_commits(e2e, tmp_path):
    case = next(c for c in e2e.build_cases(tmp_path) if c.name == "submit-refuse-owning-form-broadband")
    assert case.argv == ["submit", "home", "Broadband"]
    assert "--commit" not in case.argv
    assert case.rc == 1 and case.stdout_empty is True
    assert "action restart-broadband" in case.stderr_has
    # the live matrix runs --skip-commits: the case filter there must keep it
    assert not case.needs_commits and "commit" not in case.name
    cases = e2e.build_cases(tmp_path)
    assert case in e2e.skip_commit_cases(cases)


def test_the_dump_out_directory_refusal_case_is_read_only_and_kept_by_skip_commits(e2e, tmp_path):
    cases = e2e.build_cases(tmp_path)
    case = next(c for c in cases if c.name == "dump-refuse-out-directory")
    assert case.argv == ["dump", "--out", str(tmp_path)]
    assert case.rc == 2 and case.stdout_empty is True and case.stderr_empty is False
    assert "nothing was written" in case.stderr_has
    assert not case.needs_commits and "commit" not in case.name
    assert case in e2e.skip_commit_cases(cases)


def test_the_opener_assignment_refusal_case_takes_the_live_button_and_is_kept_by_skip_commits(e2e, tmp_path):
    cases = e2e.build_cases(tmp_path)
    case = next(c for c in cases if c.name == "submit-refuse-opener-assignment")
    assert case.rc == 1 and case.stdout_empty is True and "only opens an editor" in case.stderr_has
    assert not case.needs_commits and "commit" not in case.name and "--commit" not in case.argv
    assert case in e2e.skip_commit_cases(cases)
    e2e.LIVE.pop(e2e.OPENER_KEY, None)
    assert case.argv_factory() is None  # no live button captured: the case reports it was not run
    payload = {"buttons": [{"name": "Enable"}, {"name": "AddDropRule"}]}
    capture = next(c for c in cases if c.name == "inspect-packetfilter-json")
    try:
        assert capture.check("", payload) is None
        assert case.argv_factory() == ["submit", "packetfilter", "AddDropRule", "x=1"]
        assert capture.check("", {"buttons": [{"name": "Enable"}]}).startswith("no add-rule button")
    finally:
        e2e.LIVE.pop(e2e.OPENER_KEY, None)


def test_run_checks_empty_stdout_and_expected_stderr(e2e, tmp_path, monkeypatch):
    import subprocess

    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="body\n", stderr="other\n")

    monkeypatch.setattr(e2e.subprocess, "run", fake)
    case = e2e.Case("x", ["restore"], rc=1, stderr_empty=False, stdout_empty=True, stderr_has=("nothing compared",))
    result = e2e.run(case, {}, tmp_path, 1)
    assert any("stdout not empty" in p for p in result.problems)
    assert any("stderr is missing" in p for p in result.problems)


# --- quick mode: the same run minus what the gateway makes slow -------------------------------------


def test_quick_mode_limits_the_traversals_drops_json_tab_twins_and_shortens_the_pacing(e2e, tmp_path):
    full = e2e.build_cases(tmp_path)
    quick = e2e.quick_cases(full)
    by_name = {c.name: c for c in quick}
    # the four full-site traversals still run, over a few pages instead of all 37
    for name in ("sweep-json", "scan-json", "audit-json", "audit-text"):
        argv = by_name[name].argv
        assert argv[argv.index("--pages") + 1] == e2e.QUICK_TRAVERSAL_PAGES, name
        assert by_name[name].delay == e2e.QUICK_DELAY_S
    # a traversal that already names its pages keeps them
    assert by_name["schema-json-2pages"].argv.count("--pages") == 1
    assert by_name["sweep-out"].argv[by_name["sweep-out"].argv.index("--pages") + 1] == "sysinfo,diag"
    # every section tab keeps its text case; the JSON twin of each is dropped, as is device-status-json
    tab_text = [c for c in full if c.name.startswith("tab-") and not c.name.endswith("-json")]
    assert tab_text and all(c.name in by_name for c in tab_text)
    assert not any(c.name.startswith("tab-") and c.name.endswith("-json") for c in quick)
    assert "device-status" in by_name and "device-status-json" not in by_name
    # the per-CGI-id page reads are the only coverage of each page id: all kept
    page_json = [c.name for c in full if c.name.startswith("page-") and c.name.endswith("-json")]
    assert page_json and all(name in by_name for name in page_json)
    # the default pacing shrinks; the long pauses the gateway needs after diagnostics and the speed test stay
    assert by_name["check"].delay == e2e.QUICK_DELAY_S
    assert by_name["diag-ping-commit"].delay == 2.0 and by_name["action-commit-speed-test"].delay == 5.0
    # the guarded dosprotect pair and the refusals are untouched
    untouched = (
        e2e.TOGGLE_CASE, *e2e.RESTORE_CASES, "submit-refuse-owning-form-broadband", "dump-refuse-out-directory",
    )
    for name in untouched:
        assert name in by_name, name
    assert len(quick) < len(full)


def test_quick_mode_leaves_the_full_case_list_alone(e2e, tmp_path):
    full = e2e.build_cases(tmp_path)
    before = [(c.name, list(c.argv), c.delay) for c in full]
    e2e.quick_cases(full)
    assert [(c.name, list(c.argv), c.delay) for c in full] == before


def test_quick_mode_composes_with_skip_commits_and_names_itself_in_the_report(e2e, tmp_path, monkeypatch, capsys):
    import subprocess
    import sys

    seen = []

    def fake(argv, **kwargs):
        seen.append(argv[1:])
        return subprocess.CompletedProcess(argv, 0, stdout="Reachable\n", stderr="")

    monkeypatch.setattr(e2e.subprocess, "run", fake)
    monkeypatch.setattr(e2e.time, "sleep", lambda *_: None)
    monkeypatch.setattr(e2e, "BGW", sys.executable)
    monkeypatch.setenv("E2E_ACCESS_CODE", "x")
    out = tmp_path / "run"
    e2e.main(["--quick", "--skip-commits", "--only", "check", "--out", str(out)])
    assert seen and all("--commit" not in argv for argv in seen)
    report = (out / "report.md").read_text()
    assert "mode: quick" in report
    assert "mode: quick" in capsys.readouterr().out


def test_the_default_report_names_the_full_mode(e2e, tmp_path, monkeypatch, capsys):
    import subprocess
    import sys

    monkeypatch.setattr(
        e2e.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="Reachable\n", stderr="")
    )
    monkeypatch.setattr(e2e.time, "sleep", lambda *_: None)
    monkeypatch.setattr(e2e, "BGW", sys.executable)
    monkeypatch.setenv("E2E_ACCESS_CODE", "x")
    out = tmp_path / "run"
    e2e.main(["--skip-commits", "--only", "check", "--out", str(out)])
    assert "mode: full" in (out / "report.md").read_text()


def test_docstring_describes_quick_mode(e2e):
    assert "--quick" in e2e.__doc__


# --- the dosprotect toggle and its restore are one guarded operation --------------------------------


class FakeGateway:
    """Stands in for the bgwcli subprocess: a dosprotect select that `set --commit` really changes."""

    def __init__(self, e2e, value="on", fail_set=False):
        self.e2e = e2e
        self.value = value
        self.fail_set = fail_set
        self.calls = []

    def __call__(self, argv, **kwargs):
        import json
        import subprocess

        cmd = argv[1:]
        self.calls.append(cmd)
        field = self.e2e.DOSPROTECT_FIELD
        if cmd[:2] == ["page", "dosprotect"]:
            body = json.dumps({"selects": [{"name": field, "value": self.value}]})
            return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
        if cmd[:2] == ["set", "dosprotect"] and "--commit" in cmd:
            if not self.fail_set:
                self.value = cmd[2].split("=", 1)[1]
            return subprocess.CompletedProcess(argv, 0, stdout="Verified   yes\n", stderr="")
        if cmd[:2] == ["set", "dosprotect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="dry-run DOSPROTECT\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def commits(self):
        return [c for c in self.calls if c[:2] == ["set", "dosprotect"] and "--commit" in c]


def _run_dosprotect(e2e, tmp_path, monkeypatch, gateway, write_private=None):
    import sys

    monkeypatch.setattr(e2e.subprocess, "run", gateway)
    monkeypatch.setattr(e2e.time, "sleep", lambda *_: None)
    monkeypatch.setattr(e2e, "BGW", sys.executable)
    monkeypatch.setattr(e2e, "LIVE", {})
    monkeypatch.setenv("E2E_ACCESS_CODE", "x")
    if write_private is not None:
        monkeypatch.setattr(e2e, "write_private", write_private)
    return e2e.main(["--only", "dosprotect", "--out", str(tmp_path / "run")])


def test_dosprotect_cycle_toggles_verifies_restores_verifies_and_exits_zero(e2e, tmp_path, monkeypatch, capsys):
    gateway = FakeGateway(e2e, value="on")
    assert _run_dosprotect(e2e, tmp_path, monkeypatch, gateway) == 0
    assert [c[2] for c in gateway.commits()] == [f"{e2e.DOSPROTECT_FIELD}=off", f"{e2e.DOSPROTECT_FIELD}=on"]
    assert gateway.value == "on"
    assert "restore performed and verified" in capsys.readouterr().out


def test_artifact_write_failure_after_the_toggle_still_restores_and_reports_both(e2e, tmp_path, monkeypatch, capsys):
    import errno

    real = e2e.write_private

    def full_disk(path, text):
        if path.name.endswith(f"-{e2e.TOGGLE_CASE}.out"):
            raise OSError(errno.ENOSPC, "No space left on device")
        real(path, text)

    gateway = FakeGateway(e2e, value="on")
    assert _run_dosprotect(e2e, tmp_path, monkeypatch, gateway, full_disk) != 0
    assert gateway.value == "on"  # the restore ran in spite of the failed artifact write
    assert len(gateway.commits()) == 2
    captured = capsys.readouterr()
    assert "artifact write failed" in captured.out and f"{e2e.TOGGLE_CASE}.out" in captured.out
    assert "restore performed and verified" in captured.out


def test_an_unverifiable_restore_is_reported_explicitly_with_a_distinct_exit(e2e, tmp_path, monkeypatch, capsys):
    class RestoreDoesNotStick(FakeGateway):
        def __call__(self, argv, **kwargs):
            cmd = argv[1:]
            if cmd[:2] == ["set", "dosprotect"] and "--commit" in cmd and cmd[2].endswith("=on"):
                self.calls.append(cmd)  # acknowledged, but the live value stays toggled
                import subprocess

                return subprocess.CompletedProcess(argv, 0, stdout="Verified   yes\n", stderr="")
            return super().__call__(argv, **kwargs)

    gateway = RestoreDoesNotStick(e2e, value="on")
    assert _run_dosprotect(e2e, tmp_path, monkeypatch, gateway) == e2e.EXIT_RESTORE_UNVERIFIED
    err = capsys.readouterr().err
    assert "dosprotect restore not verified" in err and "set dosprotect" in err


def _cut_short_gateway(e2e, exc_type, restore_sticks):
    """A gateway whose first dosprotect read after the toggle raises `exc_type`; with `restore_sticks`
    False the restore commit is acknowledged but the live value stays toggled."""
    import subprocess

    class CutShort(FakeGateway):
        raised = False

        def __call__(self, argv, **kwargs):
            cmd = argv[1:]
            toggled_once = self.value != "on" and len(self.commits()) == 1
            if cmd[:2] == ["page", "dosprotect"] and toggled_once and not self.raised:
                self.raised = True
                raise exc_type("cut short")
            if not restore_sticks and cmd[:2] == ["set", "dosprotect"] and "--commit" in cmd and cmd[2].endswith("=on"):
                self.calls.append(cmd)
                return subprocess.CompletedProcess(argv, 0, stdout="Verified   yes\n", stderr="")
            return super().__call__(argv, **kwargs)

    return CutShort(e2e, value="on")


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, OSError])
def test_the_restore_runs_and_is_reported_when_the_loop_is_cut_short_after_the_toggle(
    e2e, tmp_path, monkeypatch, capsys, exc_type
):
    gateway = _cut_short_gateway(e2e, exc_type, restore_sticks=True)
    with pytest.raises(exc_type):
        _run_dosprotect(e2e, tmp_path, monkeypatch, gateway)
    assert gateway.value == "on"
    assert len(gateway.commits()) == 2
    captured = capsys.readouterr()
    assert f"restore performed and verified ({e2e.DOSPROTECT_FIELD}=on)" in captured.out
    assert (tmp_path / "run" / "report.md").exists()


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, OSError])
def test_an_unverified_restore_after_a_cut_short_loop_exits_3_instead_of_raising(
    e2e, tmp_path, monkeypatch, capsys, exc_type
):
    gateway = _cut_short_gateway(e2e, exc_type, restore_sticks=False)
    assert _run_dosprotect(e2e, tmp_path, monkeypatch, gateway) == e2e.EXIT_RESTORE_UNVERIFIED
    captured = capsys.readouterr()
    assert "dosprotect restore not verified" in captured.err and "set dosprotect" in captured.err
    assert "dosprotect restore not verified" not in captured.out
    assert "dosprotect restore not verified" in (tmp_path / "run" / "report.md").read_text()
    assert gateway.value == "off" and len(gateway.commits()) == 2  # the restore ran, but did not stick
