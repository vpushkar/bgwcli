"""Installer, deploy notes and ignore rules say what the README says."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _stub_bin(tmp_path: Path, *, with_git: bool) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for name in ["uv", *(["git"] if with_git else [])]:
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir, log


def _run_installer(tmp_path: Path, *, with_git: bool, repo: str | None = None):
    bin_dir, log = _stub_bin(tmp_path, with_git=with_git)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    if repo is not None:
        env["BGWCLI_REPO"] = repo
    done = subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=60
    )
    return done, (log.read_text() if log.exists() else "")


def test_installer_stops_with_a_clear_message_when_git_is_missing(tmp_path):
    done, calls = _run_installer(tmp_path, with_git=False)
    assert done.returncode != 0
    assert "git" in done.stderr.lower() and "required" in done.stderr.lower()
    assert "tool install" not in calls


def test_installer_proceeds_when_git_is_present(tmp_path):
    done, calls = _run_installer(tmp_path, with_git=True)
    assert done.returncode == 0, done.stderr
    assert "tool install" in calls


def test_installer_does_not_need_git_for_a_non_git_source(tmp_path):
    done, calls = _run_installer(tmp_path, with_git=False, repo="/some/local/checkout")
    assert done.returncode == 0, done.stderr
    assert "tool install" in calls


def test_deploy_readme_uses_a_user_placeholder_not_a_specific_account():
    text = (ROOT / "deploy" / "README.md").read_text()
    assert "loginctl enable-linger <user>" in text
    assert "`pi`" not in text and "enable-linger pi" not in text


def test_gitignore_and_readme_agree_on_the_dumps_directory():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert "dumps/" in ignore and "/dumps/" not in ignore
    assert "`dumps/`" in (ROOT / "README.md").read_text()
    assert os.path.exists(ROOT / ".gitignore")


def test_readme_says_the_installer_needs_git():
    assert "also need `git` on your PATH" in (ROOT / "README.md").read_text()


def test_readme_states_the_unit_start_timeout_the_unit_carries():
    unit = (ROOT / "deploy" / "bgw-autorestore.service").read_text()
    match = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.MULTILINE)
    assert match
    assert f"allows {match.group(1)} s (`TimeoutStartSec`)" in (ROOT / "README.md").read_text()


def test_readme_lists_the_client_radio_actions_with_the_actions_that_skip_the_answer_read():
    readme = (ROOT / "README.md").read_text()
    assert (
        "the actions that take down the client's own radio (`restart-wifi-2.4`, `restart-wifi-5` and "
        "`find-best-channel-5`)"
    ) in readme
