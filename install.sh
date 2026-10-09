#!/bin/sh
# bgwcli installer for macOS and Linux (x86_64 / arm64, incl. Raspberry Pi).
# Installs uv if missing, then installs bgwcli as an isolated uv tool on the system Python when it
# is 3.10 or newer (otherwise uv fetches a Python). Set BGWCLI_PYTHON=3.12 to pick a version.
# Re-run to upgrade.
#   curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
#   sh install.sh binary      (or: curl -fsSL .../install.sh | sh -s binary)
#     builds a single-file executable with PyInstaller on this machine, under a Python already
#     installed here (3.10+, one PyInstaller supports) in a throwaway venv (pip's and PyInstaller's
#     caches live there too), and puts it at ~/.local/bin/bgwcli; nothing else is downloaded or stays
#     installed (no uv). One build per
#     machine type: the file runs only on the OS and CPU it was built on. Re-run to rebuild/upgrade.
#   sh install.sh uninstall   (or: curl -fsSL .../install.sh | sh -s uninstall)
#     removes the uv tool and/or the single-file executable (a pip/pipx launcher at the same path is
#     left to pip/pipx) and LISTS the per-user state bgwcli may have written; it deletes none of that
#     state.
set -eu
MODE="${1:-}"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != uninstall ] && [ "$1" != binary ]; }; then
  echo "usage: sh install.sh [binary|uninstall]" >&2
  exit 2
fi
BIN_DIR="$HOME/.local/bin"
# True for the single-file executable this installer writes: a regular file (uv's tool install leaves a
# symlink at the same path) that does not start with `#!` (a console script from `pip install --user`
# or pipx is a `#!` text file at the same path and belongs to pip/pipx, not to this installer).
is_bgwcli_executable() { # path
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  case "$(head -c 2 "$1" 2>/dev/null)" in '#!') return 1 ;; esac
  return 0
}
is_pip_launcher() { # path
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  case "$(head -c 2 "$1" 2>/dev/null)" in '#!') return 0 ;; esac
  return 1
}
if [ "$MODE" = uninstall ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "Running: uv tool uninstall bgwcli"
    if ! uv tool uninstall bgwcli; then
      echo "uv tool uninstall bgwcli did not succeed (usually: not installed as a uv tool); its output is above."
    fi
  else
    echo "uv not found: bgwcli was not installed by this installer's uv path. If you installed it another way, use:"
    echo "  pipx uninstall bgwcli"
    echo "  pip uninstall bgwcli"
  fi
  # The single-file executable is a regular non-script file at this path; the uv tool leaves a symlink
  # there, which `uv tool uninstall` above already removed (or leaves to uv when it is still installed),
  # and a pip/pipx console script is left to pip/pipx.
  if is_bgwcli_executable "$BIN_DIR/bgwcli"; then
    rm -f "$BIN_DIR/bgwcli"
    echo "Removed the single-file executable $BIN_DIR/bgwcli"
  elif is_pip_launcher "$BIN_DIR/bgwcli"; then
    echo "$BIN_DIR/bgwcli is a script installed by pip or pipx, not this installer's executable; it was left in place (pipx uninstall bgwcli / pip uninstall bgwcli)"
  fi
  show() { # path, label
    if [ -e "$1" ]; then echo "  '$1' (present)  $2"; else echo "  '$1' (absent)  $2"; fi
  }
  : "${HOME:?HOME is not set; the state paths below are under it}"
  echo
  echo "Per-user state bgwcli may have written:"
  show "${BGW_SESSION_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/bgw}" "session cache and pool cooldown"
  show "${XDG_STATE_HOME:-$HOME/.local/state}/bgw" "dumps, recovery records, checkpoints"
  if [ -n "${BGW_DUMP_DIR:-}" ]; then
    show "$BGW_DUMP_DIR" "dump directory (BGW_DUMP_DIR)"
  fi
  echo "Autorestore timer files, if deployed (remove the timer FIRST as deploy/README.md \"Removing it\" describes:"
  echo "  systemctl --user disable --now bgw-autorestore.timer, delete the three files, systemctl --user daemon-reload):"
  show "$HOME/.config/systemd/user/bgw-autorestore.service" "autorestore unit"
  show "$HOME/.config/systemd/user/bgw-autorestore.timer" "autorestore timer"
  show "$HOME/.config/bgw/autorestore.env" "autorestore environment"
  show "$HOME/bgw-baseline.json" "baseline dump the unit references"
  echo
  echo "Nothing above was deleted; remove what you no longer want by hand."
  exit 0
fi
ORIG_PATH="$PATH"
REPO="${BGWCLI_REPO:-git+https://github.com/vpushkar/bgwcli}"
PY="${BGWCLI_PYTHON:-}"
# git is checked first, before anything is installed: `command -v git` is true for the macOS
# xcode-select stub, which fails when run, so the check runs `git --version`.
case "$REPO" in
  git+*)
    if ! git --version >/dev/null 2>&1; then
      echo "git is required to install from $REPO but does not work on this machine (missing, or an unusable stub); install git and re-run." >&2
      exit 1
    fi
    ;;
esac
ensure_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found; installing it to ~/.local/bin ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
}
if [ "$MODE" = binary ]; then
  # Build on this machine with a Python it already has - nothing is downloaded except PyInstaller,
  # into a throwaway venv under the temporary directory, and nothing stays installed afterwards.
  # Which Pythons PyInstaller supports is its own business (its package metadata; pip enforces it),
  # so the machine's default python3 is tried first and the other installed interpreters after it;
  # bgwcli itself needs 3.10 or newer. BGWCLI_PYTHON=3.12 names one candidate. No uv here: when no
  # installed Python works the build stops and says what is missing. The result embeds its Python.
  TMP="$(mktemp -d)"
  # A signal trap returns to the script in POSIX sh, so INT/TERM exit explicitly and the EXIT trap does
  # the one cleanup; otherwise an interrupted build went on to replace the old install.
  trap 'rm -rf "$TMP"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # pip's download cache and PyInstaller's cache live in the temporary directory too (both tools honour
  # these), so that the executable really is the only thing the build leaves behind.
  export PIP_CACHE_DIR="$TMP/pip-cache" PYINSTALLER_CONFIG_DIR="$TMP/pyinstaller"
  # BGWCLI_REPO may pin a ref the way uv/pip spell it: git+<remote>@<tag-or-commit>. The ref is the
  # part after the last `@` only when that `@` follows the last `/` (ssh://git@host/x/y has no ref).
  SRC="${REPO#git+}"
  tail="${SRC##*/}"
  case "$tail" in
    *@*) REF="${tail##*@}"; REMOTE="${SRC%@"$REF"}" ;;
    *) REF=""; REMOTE="$SRC" ;;
  esac
  if [ -d "$REMOTE" ]; then
    echo "Building bgwcli from the local tree $REMOTE ..."
    mkdir -p "$TMP/src" && cp -R "$REMOTE/." "$TMP/src/"
  elif [ -n "$REF" ]; then
    # a commit id needs the full history to be checked out, so no --depth here
    echo "Fetching bgwcli from $REMOTE at $REF ..."
    git clone -q "$REMOTE" "$TMP/src" && git -C "$TMP/src" checkout -q "$REF"
  else
    echo "Fetching bgwcli from $REMOTE ..."
    git clone -q --depth 1 "$REMOTE" "$TMP/src"
  fi
  BUILD_PY_BIN=""
  if [ -n "$PY" ]; then
    candidates="python$PY"
  else
    candidates="python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10"
  fi
  for candidate in $candidates; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null \
       && "$candidate" -m venv "$TMP/venv" 2>/dev/null \
       && "$TMP/venv/bin/python" -m pip -q install pyinstaller 2>/dev/null; then
      BUILD_PY_BIN="$candidate"
      break
    fi
    rm -rf "$TMP/venv"
  done
  if [ -z "$BUILD_PY_BIN" ]; then
    echo "No Python on this machine can build the executable: bgwcli needs 3.10 or newer, PyInstaller must install under it (pip checks PyInstaller's supported versions), and 'python -m venv' must work (Debian/Ubuntu: apt install python3-venv). Tried: $candidates. Name one with BGWCLI_PYTHON=3.12, or use the default install (no 'binary'), which brings its own Python through uv." >&2
    exit 1
  fi
  echo "Building the single-file executable with PyInstaller under $BUILD_PY_BIN ($("$BUILD_PY_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')) ..."
  (cd "$TMP/src" && "$TMP/venv/bin/pyinstaller" --onefile --name bgwcli --paths src --distpath "$TMP/dist" \
      --workpath "$TMP/build" --specpath "$TMP" --log-level WARN scripts/pyinstaller_launcher.py)
  # The new executable proves it starts BEFORE the old install is touched: a broken build leaves the
  # uv tool, its symlink and any previous executable exactly as they were.
  if ! "$TMP/dist/bgwcli" help >/dev/null; then
    echo "The built executable does not start; nothing was replaced (the build output is above)." >&2
    exit 1
  fi
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^bgwcli '; then
    echo "Removing the uv tool install so that only one bgwcli is on your PATH ..."
    uv tool uninstall bgwcli
  fi
  mkdir -p "$BIN_DIR"
  if [ -L "$BIN_DIR/bgwcli" ]; then rm -f "$BIN_DIR/bgwcli"; fi
  if is_pip_launcher "$BIN_DIR/bgwcli"; then
    echo "Note: $BIN_DIR/bgwcli was a pip/pipx launcher; it is replaced by the executable, but the pip package itself is still installed (pip uninstall bgwcli / pipx uninstall bgwcli)."
  fi
  install -m 0755 "$TMP/dist/bgwcli" "$BIN_DIR/bgwcli"
  echo
  echo "Installed: $BIN_DIR/bgwcli  (single-file executable; built for this OS and CPU only; re-run to rebuild)"
  case ":$ORIG_PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      echo "$BIN_DIR is not on your PATH yet. For this shell:   export PATH=\"$BIN_DIR:\$PATH\""
      echo "Permanently:   echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.profile   (or ~/.zshrc / ~/.bashrc)"
      ;;
  esac
  echo "Try:  bgwcli --help"
  exit 0
fi
ensure_uv
if [ -n "$PY" ]; then
  echo "Installing bgwcli from $REPO with Python $PY ..."
  uv tool install --force --python "$PY" "$REPO"
else
  echo "Installing bgwcli from $REPO (system Python 3.10+ when present, otherwise one uv fetches) ..."
  # uv otherwise prefers a managed Python it already downloaded (the old installer left one behind).
  UV_PYTHON_PREFERENCE=system uv tool install --force "$REPO"
fi
BIN="$(uv tool dir --bin 2>/dev/null || true)"
[ -n "$BIN" ] || BIN="$BIN_DIR"
# When uv's bin dir is not ~/.local/bin (UV_TOOL_BIN_DIR, XDG settings) the tool's symlink does not
# overwrite a single-file executable there, which would stay first on PATH; remove the one this
# installer wrote (never a pip/pipx launcher). In the default dir uv's --force already replaced it.
if [ "$BIN" != "$BIN_DIR" ] && is_bgwcli_executable "$BIN_DIR/bgwcli"; then
  rm -f "$BIN_DIR/bgwcli"
  echo "Removed the single-file executable $BIN_DIR/bgwcli so that only one bgwcli is on your PATH"
fi
echo
echo "Installed: $BIN/bgwcli"
case ":$ORIG_PATH:" in
  *":$BIN:"*) ;;
  *)
    echo "$BIN is not on your PATH yet. For this shell:   export PATH=\"$BIN:\$PATH\""
    echo "Permanently:   echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile   (or ~/.zshrc / ~/.bashrc)"
    ;;
esac
echo "Try:  bgwcli --help"
