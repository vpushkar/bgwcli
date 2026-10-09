"""`install.sh uninstall` removes the tool, lists the per-user state it may have written, and deletes none of it."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOSING = "Nothing above was deleted; remove what you no longer want by hand."


def _stub(bin_dir: Path, log: Path, name: str, *, exit_code: int = 0) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {exit_code}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, *args: str, with_uv: bool = True, uv_exit: int = 0, env_extra=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    home = tmp_path / "home"
    home.mkdir()
    if with_uv:
        _stub(bin_dir, log, "uv", exit_code=uv_exit)
    _stub(bin_dir, log, "git")
    _stub(bin_dir, log, "curl")
    env = {"PATH": str(bin_dir), "HOME": str(home)}
    env.update(env_extra or {})
    done = subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, (log.read_text() if log.exists() else ""), home


def _line_for(out: str, needle: str) -> str:
    matches = [ln for ln in out.splitlines() if needle in ln]
    assert matches, f"no line mentioning {needle!r} in:\n{out}"
    return matches[0]


def test_uninstall_runs_uv_tool_uninstall_and_lists_absent_state(tmp_path):
    done, calls, home = _run(tmp_path, "uninstall")
    assert done.returncode == 0, done.stderr
    assert calls.splitlines() == ["uv tool uninstall bgwcli"]
    out = done.stdout
    for needle in (
        f"{home}/.cache/bgw",
        f"{home}/.local/state/bgw",
        f"{home}/.config/systemd/user/bgw-autorestore.service",
        f"{home}/.config/systemd/user/bgw-autorestore.timer",
        f"{home}/.config/bgw/autorestore.env",
        f"{home}/bgw-baseline.json",
    ):
        assert "(absent)" in _line_for(out, needle)
    assert "systemctl --user disable --now bgw-autorestore.timer" in out
    assert CLOSING in out


def test_uninstall_reports_present_state_and_never_deletes_it(tmp_path):
    home = tmp_path / "home"
    cache = tmp_path / "xdgcache"
    state = tmp_path / "xdgstate"
    dumps = tmp_path / "dumps"
    paths_dirs = [cache / "bgw", state / "bgw", dumps]
    for d in paths_dirs:
        d.mkdir(parents=True)
        (d / "keep.json").write_text("x")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    files = [
        unit_dir / "bgw-autorestore.service",
        unit_dir / "bgw-autorestore.timer",
        home / ".config" / "bgw" / "autorestore.env",
        home / "bgw-baseline.json",
    ]
    (home / ".config" / "bgw").mkdir(parents=True)
    for f in files:
        f.write_text("x")
    # _run creates HOME with mkdir(); pre-created home is fine only if it does not exist, so
    # point HOME at the pre-built tree through a fresh run directory instead.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_dir, log, "uv")
    done = subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh"), "uninstall"],
        env={
            "PATH": str(bin_dir),
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "XDG_STATE_HOME": str(state),
            "BGW_DUMP_DIR": str(dumps),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    for p in [*paths_dirs, *files]:
        assert "(present)" in _line_for(out, str(p)), p
        assert p.exists()
    assert (cache / "bgw" / "keep.json").exists() and (dumps / "keep.json").exists()
    assert CLOSING in out


def test_session_cache_override_wins_over_xdg(tmp_path):
    override = tmp_path / "sess"
    done, _calls, _home = _run(
        tmp_path,
        "uninstall",
        env_extra={"BGW_SESSION_CACHE_DIR": str(override), "XDG_CACHE_HOME": str(tmp_path / "x")},
    )
    assert done.returncode == 0
    assert "(absent)" in _line_for(done.stdout, str(override))
    assert str(tmp_path / "x" / "bgw") not in done.stdout


def test_uninstall_without_uv_names_alternatives_and_still_lists(tmp_path):
    done, calls, home = _run(tmp_path, "uninstall", with_uv=False)
    assert done.returncode == 0, done.stderr
    assert "uv" not in calls and "curl" not in calls
    assert "pipx uninstall bgwcli" in done.stdout and "pip uninstall bgwcli" in done.stdout
    assert f"{home}/.cache/bgw" in done.stdout
    assert CLOSING in done.stdout


def test_uninstall_when_tool_absent_continues_with_listing(tmp_path):
    done, calls, home = _run(tmp_path, "uninstall", uv_exit=1)
    assert done.returncode == 0, done.stderr
    assert calls.splitlines() == ["uv tool uninstall bgwcli"]
    assert "not installed" in done.stdout
    assert f"{home}/.local/state/bgw" in done.stdout
    assert CLOSING in done.stdout


def test_unknown_argument_is_a_usage_error(tmp_path):
    done, calls, _home = _run(tmp_path, "frobnicate")
    assert done.returncode == 2
    assert "usage" in done.stderr.lower() and "uninstall" in done.stderr
    assert calls == ""


def test_an_extra_argument_after_uninstall_is_a_usage_error(tmp_path):
    done, calls, _home = _run(tmp_path, "uninstall", "extra")
    assert done.returncode == 2
    assert "usage" in done.stderr.lower()
    assert calls == ""  # nothing ran, in particular not the uv uninstall


def test_plain_run_still_installs(tmp_path):
    done, calls, _home = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "tool install" in calls
