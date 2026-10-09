#!/bin/sh
# bgwcli installer for macOS and Linux (x86_64 / arm64, incl. Raspberry Pi).
# Installs uv if missing, then installs bgwcli as an isolated uv tool on the system Python when it
# is 3.10 or newer (otherwise uv fetches a Python). Set BGWCLI_PYTHON=3.12 to pick a version.
# Re-run to upgrade.
#   curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
#   sh install.sh uninstall   (or: curl -fsSL .../install.sh | sh -s uninstall)
#     removes the uv tool and LISTS the per-user state bgwcli may have written; it deletes none of it.
set -eu
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != uninstall ]; }; then
  echo "usage: sh install.sh [uninstall]" >&2
  exit 2
fi
if [ "${1:-}" = uninstall ]; then
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
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; installing it to ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ -n "$PY" ]; then
  echo "Installing bgwcli from $REPO with Python $PY ..."
  uv tool install --force --python "$PY" "$REPO"
else
  echo "Installing bgwcli from $REPO (system Python 3.10+ when present, otherwise one uv fetches) ..."
  # uv otherwise prefers a managed Python it already downloaded (the old installer left one behind).
  UV_PYTHON_PREFERENCE=system uv tool install --force "$REPO"
fi
BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
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
