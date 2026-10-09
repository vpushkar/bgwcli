"""The installer checks that git works before it installs anything (an xcode-select stub is on PATH but fails)."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub(bin_dir: Path, log: Path, name: str, *, exit_code: int = 0) -> None:
    path = bin_dir / name
    # The uv stub also records the UV_PYTHON_PREFERENCE it was started with, on the line after the call.
    marker = f'echo "uv-pref=${{UV_PYTHON_PREFERENCE:-unset}}" >> "{log}"\n' if name == "uv" else ""
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n{marker}exit {exit_code}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, *, git_exit: int | None, with_uv: bool, extra_env: dict[str, str] | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    if git_exit is not None:
        _stub(bin_dir, log, "git", exit_code=git_exit)
    if with_uv:
        _stub(bin_dir, log, "uv")
    _stub(bin_dir, log, "curl")
    done = subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh")],
        env={"PATH": str(bin_dir), "HOME": str(tmp_path), **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, (log.read_text() if log.exists() else "")


def test_a_git_stub_that_fails_stops_the_installer_before_uv_is_fetched(tmp_path):
    done, calls = _run(tmp_path, git_exit=1, with_uv=False)
    assert done.returncode != 0
    assert "git" in done.stderr.lower() and "required" in done.stderr.lower()
    assert "curl" not in calls and "tool install" not in calls
    assert "git --version" in calls


def test_a_missing_git_stops_the_installer_before_uv_is_fetched(tmp_path):
    done, calls = _run(tmp_path, git_exit=None, with_uv=False)
    assert done.returncode != 0
    assert "curl" not in calls


def test_a_working_git_lets_the_installer_fetch_uv_and_install(tmp_path):
    done, calls = _run(tmp_path, git_exit=0, with_uv=False)
    assert "git --version" in calls
    assert calls.index("git --version") < calls.index("curl")


def _install_call(calls: str) -> tuple[str, str]:
    """The recorded `uv tool install` line and the UV_PYTHON_PREFERENCE marker the stub logged after it."""
    lines = calls.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("uv tool install"))
    return lines[index], lines[index + 1]


def test_without_an_override_the_installer_prefers_the_system_python(tmp_path):
    done, calls = _run(tmp_path, git_exit=0, with_uv=True)
    assert done.returncode == 0, done.stderr
    install, preference = _install_call(calls)
    assert "--python" not in install
    assert preference == "uv-pref=system"


def test_an_override_still_pins_the_python_and_leaves_the_preference_alone(tmp_path):
    # A pin of behaviour the installer had before the preference change: BGWCLI_PYTHON always passed
    # --python and never set a preference. It guards against a regression, not a fix.
    done, calls = _run(tmp_path, git_exit=0, with_uv=True, extra_env={"BGWCLI_PYTHON": "3.12"})
    assert done.returncode == 0, done.stderr
    install, preference = _install_call(calls)
    assert "--python 3.12" in install
    assert preference == "uv-pref=unset"
