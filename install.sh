#!/bin/sh
# bgwcli installer for macOS and Linux (x86_64 / arm64, incl. Raspberry Pi).
# Installs uv if missing, then installs bgwcli as an isolated uv tool with its own Python
# (so the system Python version does not matter). Re-run to upgrade.
#   curl -fsSL https://raw.githubusercontent.com/vpushkar/bgwcli/main/install.sh | sh
set -eu
REPO="${BGWCLI_REPO:-git+https://github.com/vpushkar/bgwcli}"
PY="${BGWCLI_PYTHON:-3.12}"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; installing it to ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "Installing bgwcli from $REPO with Python $PY ..."
uv tool install --force --python "$PY" "$REPO"
BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
echo
echo "Installed: $BIN/bgwcli"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Add it to your PATH, e.g.:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.profile";;
esac
echo "Try:  bgwcli --help"
