"""`install.sh binary` builds a single-file executable with PyInstaller under a Python already on the
machine - the default python3 first, then the installed python3.15 .. python3.10; pip decides whether
PyInstaller installs under each (its Requires-Python), the installer hardcodes no support window and
never falls back to uv - in a throwaway venv, proves the build starts, and only then puts it at
~/.local/bin/bgwcli; when no Python works it exits 1 with a clear message. `install.sh uninstall` removes
that file too (never a pip/pipx launcher), and the plain mode removes it when uv links its tool elsewhere.
Driven with stubs for the Pythons, uv, git and curl: nothing is downloaded or built here."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "pyinstaller_launcher.py"
PYTHON_NAMES = ("python3", "python3.15", "python3.14", "python3.13", "python3.12", "python3.11", "python3.10")

# Writes the executable the installer expects under --distpath (the venv's pyinstaller stub); with
# PYINSTALLER_STUB_BROKEN set the "executable" exits 1, standing in for a build that does not start.
_WRITE_BINARY = """
    dist=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--distpath" ]; then dist="$2"; fi
      shift
    done
    mkdir -p "$dist"
    if [ -n "${PYINSTALLER_STUB_BROKEN:-}" ]; then
      printf '#!/bin/sh\\nexit 1\\n' > "$dist/bgwcli"
    else
      printf '#!/bin/sh\\necho stub-binary "$@"\\n' > "$dist/bgwcli"
    fi
    chmod 755 "$dist/bgwcli"
"""
UV_STUB = """#!/bin/sh
echo "uv $*" >> "$LOG"
case " $* " in
  *" tool list"*)
    if [ -n "${UV_STUB_TOOL_INSTALLED:-}" ]; then echo "bgwcli v0.1.0"; echo "- bgwcli"; fi
    ;;
  *" tool dir --bin"*) echo "${UV_STUB_BIN_DIR:-$HOME/.local/bin}" ;;
esac
exit 0
"""
# A Python stub (every one is 3.10+, so the floor check passes): `-m venv DIR` creates a venv whose
# python accepts `pip install pyinstaller` only when the stub was made "good" (standing in for pip
# enforcing PyInstaller's Requires-Python) and whose pyinstaller writes the binary; calls are logged.
PYTHON_STUB = f"""#!/bin/sh
me="$(basename "$0")"
echo "$me $*" >> "$LOG"
case "$1 $2" in
  "-c "*SystemExit*) exit 0 ;;
  "-c "*print*) echo "$PY_STUB_VERSION"; exit 0 ;;
  "-m venv")
    mkdir -p "$3/bin"
    cat > "$3/bin/python" <<'EOF'
#!/bin/sh
echo "venv-python $*" >> "$LOG"
echo "venv-python PIP_CACHE_DIR=${{PIP_CACHE_DIR:-}}" >> "$LOG"
case "$*" in
  *"pip -q install pyinstaller"*)
    # stands in for Ctrl-C / a TERM reaching the installer while pip runs: the parent is the installer
    if [ -n "${{PIP_STUB_TERM_INSTALLER:-}}" ]; then kill -TERM $PPID; fi
    exit $PY_STUB_GOOD ;;
esac
exit 0
EOF
    cat > "$3/bin/pyinstaller" <<'EOF'
#!/bin/sh
echo "venv-pyinstaller $*" >> "$LOG"
echo "venv-pyinstaller PYINSTALLER_CONFIG_DIR=${{PYINSTALLER_CONFIG_DIR:-}}" >> "$LOG"
{_WRITE_BINARY}
exit 0
EOF
    chmod 755 "$3/bin/python" "$3/bin/pyinstaller"
    exit 0 ;;
esac
exit 0
"""
GIT_STUB = f"""#!/bin/sh
echo "git $*" >> "$LOG"
if [ "$1" = "--version" ]; then echo "git version 2.0 (stub)"; exit 0; fi
if [ "$1" = "clone" ]; then
  for last; do :; done
  mkdir -p "$last/scripts" && cp "{LAUNCHER}" "$last/scripts/pyinstaller_launcher.py"
fi
exit 0
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, *args: str, env_extra=None, tool_installed=False, good_pythons=("python3",)):
    """Every Python name the installer may try is stubbed (so the machine's own interpreters are never
    consulted); PyInstaller "installs" only under the ones in `good_pythons`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)  # some tests pre-seed ~/.local/bin
    _write(bin_dir / "uv", UV_STUB)
    _write(bin_dir / "git", GIT_STUB)
    _write(bin_dir / "curl", '#!/bin/sh\necho "curl $*" >> "$LOG"\nexit 0\n')
    for name in PYTHON_NAMES:
        good = name in good_pythons
        version = "3.14.7" if name == "python3" else name.removeprefix("python") + ".9"
        stub = PYTHON_STUB.replace("$PY_STUB_GOOD", "0" if good else "1").replace("$PY_STUB_VERSION", version)
        _write(bin_dir / name, stub)
    # the real mktemp/cp/install/grep/basename are needed by the binary mode; the stubs shadow the rest
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home), "LOG": str(log)}
    if tool_installed:
        env["UV_STUB_TOOL_INSTALLED"] = "1"
    env.update(env_extra or {})
    done = subprocess.run(
        ["/bin/sh", str(ROOT / "install.sh"), *args], env=env, capture_output=True, text=True, timeout=60
    )
    return done, (log.read_text() if log.exists() else ""), home


def test_the_launcher_hands_over_to_the_cli_and_nothing_else():
    text = LAUNCHER.read_text()
    assert "from bgwcli.cli import main" in text and "sys.exit(main())" in text
    assert "from ." not in text  # a relative import has no parent in a PyInstaller script


def test_binary_mode_builds_under_the_system_python_in_a_throwaway_venv_without_uv(tmp_path):
    done, calls, home = _run(tmp_path, "binary")
    assert done.returncode == 0, done.stderr + done.stdout
    lines = calls.splitlines()
    assert "git --version" in lines
    clone = next(ln for ln in lines if ln.startswith("git clone "))
    assert "--depth 1 https://github.com/vpushkar/bgwcli " in clone + " "
    assert any(ln.startswith("python3 -m venv ") for ln in lines)  # the system default is tried first
    assert "venv-python -m pip -q install pyinstaller" in lines
    build = next(ln for ln in lines if ln.startswith("venv-pyinstaller "))
    assert "--onefile --name bgwcli --paths src" in build and build.endswith("scripts/pyinstaller_launcher.py")
    assert not any(ln.startswith("uv run") or "tool install" in ln for ln in lines)
    assert not any(ln.startswith("curl") for ln in lines)  # uv was neither needed nor installed
    assert "under python3 (3.14.7)" in done.stdout
    installed = home / ".local" / "bin" / "bgwcli"
    assert installed.is_file() and not installed.is_symlink() and installed.stat().st_mode & 0o111
    assert "Installed:" in done.stdout and "single-file executable" in done.stdout


def test_binary_mode_keeps_the_pip_and_pyinstaller_caches_inside_the_temporary_directory(tmp_path):
    # pip's download cache and PyInstaller's cache (macOS: ~/Library/Application Support/pyinstaller)
    # would otherwise survive the build outside the directory the installer deletes
    done, calls, home = _run(tmp_path, "binary")
    assert done.returncode == 0, done.stderr + done.stdout
    lines = calls.splitlines()
    clone_dir = next(ln for ln in lines if ln.startswith("git clone ")).split()[-1]
    tmp = clone_dir.rsplit("/", 1)[0]  # the mktemp directory the sources were cloned into
    pip_cache = next(ln for ln in lines if ln.startswith("venv-python PIP_CACHE_DIR=")).split("=", 1)[1]
    pyi_cache = next(ln for ln in lines if ln.startswith("venv-pyinstaller PYINSTALLER_CONFIG_DIR=")).split("=", 1)[1]
    assert pip_cache.startswith(tmp + "/") and pyi_cache.startswith(tmp + "/")
    assert not pip_cache.startswith(str(home)) and not pyi_cache.startswith(str(home))
    assert not os.path.exists(tmp)  # and the whole directory is gone afterwards


def test_binary_mode_moves_to_an_older_python_when_pyinstaller_refuses_the_newer_ones(tmp_path):
    # pip refuses PyInstaller under the newest interpreters (too new for its Requires-Python); the first
    # candidate it installs under is used, and the support window is never hardcoded by the installer
    done, calls, _home = _run(tmp_path, "binary", good_pythons=("python3.13",))
    assert done.returncode == 0, done.stderr + done.stdout
    lines = calls.splitlines()
    tried = [ln.split()[0] for ln in lines if " -m venv " in ln]
    assert tried == ["python3", "python3.15", "python3.14", "python3.13"]
    assert lines.count("venv-python -m pip -q install pyinstaller") == 4
    assert "under python3.13 (3.13.9)" in done.stdout
    assert not any(ln.startswith("python3.12") or ln.startswith("uv run") for ln in lines)


def test_binary_mode_honours_bgwcli_python_and_a_local_source_tree(tmp_path):
    src = tmp_path / "tree"
    (src / "scripts").mkdir(parents=True)
    (src / "scripts" / "pyinstaller_launcher.py").write_text(LAUNCHER.read_text())
    done, calls, _home = _run(
        tmp_path, "binary", env_extra={"BGWCLI_REPO": str(src), "BGWCLI_PYTHON": "3.12"}, good_pythons=("python3.12",)
    )
    assert done.returncode == 0, done.stderr + done.stdout
    lines = calls.splitlines()
    assert not any(ln.startswith("git clone") for ln in lines)
    assert any(ln.startswith("python3.12 -m venv ") for ln in lines)
    assert [ln.split()[0] for ln in lines if " -m venv " in ln] == ["python3.12"]  # the one named candidate
    assert f"local tree {src}" in done.stdout


def test_binary_mode_checks_out_a_tag_or_commit_pinned_with_at_in_bgwcli_repo(tmp_path):
    # BGWCLI_REPO=git+https://...@<ref> is what the plain (uv) mode and deploy/README.md document; the
    # binary mode clones the remote in full and checks the ref out instead of handing the pin to git clone
    for ref in ("v1", "abc1234"):
        (tmp_path / ref).mkdir()
        done, calls, _home = _run(
            tmp_path / ref, "binary", env_extra={"BGWCLI_REPO": f"git+https://github.com/vpushkar/bgwcli@{ref}"}
        )
        assert done.returncode == 0, done.stderr + done.stdout
        lines = calls.splitlines()
        clone = next(ln for ln in lines if ln.startswith("git clone "))
        assert "@" not in clone and "--depth" not in clone
        assert clone.split()[-2] == "https://github.com/vpushkar/bgwcli"
        checkout = next(ln for ln in lines if " checkout " in ln)
        assert checkout.startswith("git -C ") and checkout.endswith(f" checkout -q {ref}")
        assert f"Fetching bgwcli from https://github.com/vpushkar/bgwcli at {ref} ..." in done.stdout


def test_binary_mode_does_not_take_the_user_of_an_ssh_url_for_a_pin(tmp_path):
    done, calls, _home = _run(tmp_path, "binary", env_extra={"BGWCLI_REPO": "git+ssh://git@github.com/vpushkar/bgwcli"})
    assert done.returncode == 0, done.stderr + done.stdout
    lines = calls.splitlines()
    clone = next(ln for ln in lines if ln.startswith("git clone "))
    assert "--depth 1 ssh://git@github.com/vpushkar/bgwcli " in clone + " "
    assert not any(" checkout " in ln for ln in lines)


def test_binary_mode_interrupted_during_the_build_stops_instead_of_continuing_after_cleanup(tmp_path):
    # a signal trap returns to the script in POSIX sh: with the cleanup on INT/TERM the installer went on
    # to uninstall the uv tool and install whatever was built; now it exits with the signal's status
    done, calls, home = _run(tmp_path, "binary", tool_installed=True, env_extra={"PIP_STUB_TERM_INSTALLER": "1"})
    assert done.returncode == 143, done.stderr + done.stdout
    lines = calls.splitlines()
    assert "venv-python -m pip -q install pyinstaller" in lines  # the signal arrived here
    assert not any(ln.startswith("venv-pyinstaller ") for ln in lines)
    assert "uv tool uninstall bgwcli" not in lines
    assert not (home / ".local" / "bin" / "bgwcli").exists()


def test_binary_mode_stops_with_a_clear_message_when_no_installed_python_works(tmp_path):
    # no uv fallback: the binary mode never downloads a Python or uv; it names what is missing
    done, calls, home = _run(tmp_path, "binary", good_pythons=())
    assert done.returncode == 1, done.stderr + done.stdout
    lines = calls.splitlines()
    assert [ln.split()[0] for ln in lines if " -m venv " in ln] == list(PYTHON_NAMES)  # every installed candidate tried
    assert not any(ln.startswith("uv ") or ln.startswith("curl") for ln in lines)
    assert "No Python on this machine can build the executable" in done.stderr
    assert "BGWCLI_PYTHON" in done.stderr and "python3-venv" in done.stderr
    assert not (home / ".local" / "bin" / "bgwcli").exists()


def test_binary_mode_removes_a_uv_tool_install_so_only_one_bgwcli_remains(tmp_path):
    done, calls, home = _run(tmp_path, "binary", tool_installed=True)
    assert done.returncode == 0, done.stderr + done.stdout
    assert "uv tool uninstall bgwcli" in calls.splitlines()
    assert "only one bgwcli" in done.stdout
    assert (home / ".local" / "bin" / "bgwcli").is_file()


def test_binary_mode_replaces_a_leftover_uv_symlink(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("old")
    (home / ".local" / "bin" / "bgwcli").symlink_to(target)
    done, _calls, home = _run(tmp_path, "binary")
    assert done.returncode == 0, done.stderr + done.stdout
    installed = home / ".local" / "bin" / "bgwcli"
    assert installed.is_file() and not installed.is_symlink()
    assert target.read_text() == "old"  # the link was replaced, its target untouched


def test_binary_mode_keeps_the_old_install_when_the_new_executable_does_not_start(tmp_path):
    # the smoke test runs on the build output before anything is removed: a broken build leaves the uv
    # tool, its symlink and the PATH as they were
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    target = tmp_path / "uv-tools" / "bgwcli"
    target.parent.mkdir()
    target.write_text("tool")
    (home / ".local" / "bin" / "bgwcli").symlink_to(target)
    done, calls, home = _run(tmp_path, "binary", tool_installed=True, env_extra={"PYINSTALLER_STUB_BROKEN": "1"})
    assert done.returncode == 1, done.stderr + done.stdout
    assert "does not start; nothing was replaced" in done.stderr
    assert any(ln.startswith("venv-pyinstaller ") for ln in calls.splitlines())  # the build itself ran
    assert "uv tool uninstall bgwcli" not in calls.splitlines()
    assert (home / ".local" / "bin" / "bgwcli").is_symlink() and target.read_text() == "tool"
    assert "Installed:" not in done.stdout


PIP_LAUNCHER = "#!/usr/bin/python3\n# -*- coding: utf-8 -*-\nimport sys\nfrom bgwcli.cli import main\nmain()\n"
ELF_HEADER = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8  # what a PyInstaller executable starts with on Linux


def test_binary_mode_replaces_a_pip_launcher_and_says_the_package_is_still_installed(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_text(PIP_LAUNCHER)
    done, _calls, home = _run(tmp_path, "binary")
    assert done.returncode == 0, done.stderr + done.stdout
    installed = home / ".local" / "bin" / "bgwcli"
    assert "stub-binary" in installed.read_text()  # overwritten with the build
    assert "pip/pipx launcher" in done.stdout and "pip uninstall bgwcli" in done.stdout


def test_uninstall_removes_the_executable_and_still_lists_state_without_deleting_it(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_bytes(ELF_HEADER)  # the executable is never a `#!` script
    (home / ".cache" / "bgw").mkdir(parents=True)
    done, calls, home = _run(tmp_path, "uninstall")
    assert done.returncode == 0, done.stderr
    assert "uv tool uninstall bgwcli" in calls.splitlines()
    assert not (home / ".local" / "bin" / "bgwcli").exists()
    assert "Removed the single-file executable" in done.stdout
    assert (home / ".cache" / "bgw").is_dir()
    assert "(present)" in next(ln for ln in done.stdout.splitlines() if f"{home}/.cache/bgw" in ln)
    assert "Nothing above was deleted" in done.stdout


def test_uninstall_leaves_a_pip_or_pipx_launcher_in_place_and_names_their_uninstall(tmp_path):
    # a console script from `pip install --user` / pipx is a regular non-symlink file at the same path,
    # but it is a `#!` text script, not this installer's executable; it belongs to pip or pipx
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_text(PIP_LAUNCHER)
    done, _calls, home = _run(tmp_path, "uninstall")
    assert done.returncode == 0, done.stderr
    assert (home / ".local" / "bin" / "bgwcli").read_text() == PIP_LAUNCHER
    assert "Removed the single-file executable" not in done.stdout
    note = next(ln for ln in done.stdout.splitlines() if "installed by pip or pipx" in ln)
    assert "left in place" in note and "pipx uninstall bgwcli" in note and "pip uninstall bgwcli" in note


def test_uninstall_leaves_a_uv_symlink_to_uv(tmp_path):
    # the uv tool's symlink is uv's to remove (`uv tool uninstall` above); the installer only removes the file it wrote
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    target = tmp_path / "uv-tools" / "bgwcli"
    target.parent.mkdir()
    target.write_text("tool")
    (home / ".local" / "bin" / "bgwcli").symlink_to(target)
    done, _calls, home = _run(tmp_path, "uninstall")
    assert done.returncode == 0, done.stderr
    assert (home / ".local" / "bin" / "bgwcli").is_symlink()
    assert "Removed the single-file executable" not in done.stdout


def test_plain_mode_still_installs_the_uv_tool_and_never_touches_the_pythons(tmp_path):
    done, calls, _home = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert any("tool install" in ln for ln in calls.splitlines())
    assert not any(ln.startswith("python") for ln in calls.splitlines())


def test_plain_mode_removes_the_executable_when_uv_installs_its_link_somewhere_else(tmp_path):
    # with UV_TOOL_BIN_DIR (or a non-default `uv tool dir --bin`) the tool's symlink does not overwrite
    # ~/.local/bin/bgwcli, which then stays first on PATH; the installer removes its own executable there
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_bytes(ELF_HEADER)
    done, calls, home = _run(tmp_path, env_extra={"UV_STUB_BIN_DIR": f"{home}/bin"})
    assert done.returncode == 0, done.stderr
    assert any("tool install" in ln for ln in calls.splitlines())
    assert not (home / ".local" / "bin" / "bgwcli").exists()
    removed = next(ln for ln in done.stdout.splitlines() if ln.startswith("Removed the single-file executable "))
    assert f"{home}/.local/bin/bgwcli so that only one bgwcli is on your PATH" in removed
    assert f"Installed: {home}/bin/bgwcli" in done.stdout


def test_plain_mode_leaves_the_executable_in_the_default_bin_dir_to_uv(tmp_path):
    # default dir: uv's own `tool install --force` replaces the file with its symlink (the stub does
    # not), so the installer removes nothing itself
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_bytes(ELF_HEADER)
    done, _calls, home = _run(tmp_path)  # the uv stub answers `tool dir --bin` with ~/.local/bin
    assert done.returncode == 0, done.stderr
    assert (home / ".local" / "bin" / "bgwcli").read_bytes() == ELF_HEADER
    assert "Removed the single-file executable" not in done.stdout


def test_plain_mode_never_removes_a_pip_launcher_even_when_uv_installs_elsewhere(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "bgwcli").write_text(PIP_LAUNCHER)
    done, _calls, home = _run(tmp_path, env_extra={"UV_STUB_BIN_DIR": f"{home}/bin"})
    assert done.returncode == 0, done.stderr
    assert (home / ".local" / "bin" / "bgwcli").read_text() == PIP_LAUNCHER
    assert "Removed the single-file executable" not in done.stdout


def test_binary_with_an_extra_argument_is_a_usage_error(tmp_path):
    done, calls, _home = _run(tmp_path, "binary", "now")
    assert done.returncode == 2 and "usage:" in done.stderr and "binary|uninstall" in done.stderr
    assert calls == ""
