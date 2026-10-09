"""``python -m bgwcli`` runs the CLI; importing the module does not."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def test_importing_the_main_module_does_not_run_the_cli(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bgwcli", "--no-such-flag"])
    sys.modules.pop("bgwcli.__main__", None)
    module = importlib.import_module("bgwcli.__main__")  # would raise SystemExit if it ran main()
    assert callable(module.main)


def test_python_dash_m_runs_the_cli(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    done = subprocess.run(
        [sys.executable, "-m", "bgwcli", "--help"], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0
    assert "bgwcli" in done.stdout.lower() or "usage" in done.stdout.lower()
