"""Tests for scripts/update_readme_pages.py marker validation and rewrite."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("update_readme_pages", ROOT / "scripts" / "update_readme_pages.py")
assert _spec is not None and _spec.loader is not None
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

BLOCKS = {"a": "NEW-A\n", "b": "NEW-B\n"}
GOOD = (
    "intro\n<!-- a:start -->\nold a\n<!-- a:end -->\nmiddle\n"
    "<!-- b:start -->\nold b\n<!-- b:end -->\noutro\n"
)


def _fake_blocks(monkeypatch):
    monkeypatch.setattr(script.helpdoc, "README_BLOCKS", {"a": "ra", "b": "rb"})
    monkeypatch.setattr(script.helpdoc, "ra", lambda: "NEW-A\n", raising=False)
    monkeypatch.setattr(script.helpdoc, "rb", lambda: "NEW-B\n", raising=False)


def test_rewrite_well_formed():
    out = script.rewrite(GOOD, BLOCKS)
    assert out == (
        "intro\n<!-- a:start -->\nNEW-A\n<!-- a:end -->\nmiddle\n"
        "<!-- b:start -->\nNEW-B\n<!-- b:end -->\noutro\n"
    )


def _errors(text: str) -> str:
    with pytest.raises(ValueError) as exc:
        script.rewrite(text, BLOCKS)
    return str(exc.value)


def test_duplicate_start_rejected():
    text = "<!-- a:start -->\nHAND\n<!-- a:start -->\nx\n<!-- a:end -->\n<!-- b:start -->\n<!-- b:end -->\n"
    msg = _errors(text)
    assert "a" in msg and "start" in msg


def test_duplicate_end_rejected():
    text = "<!-- a:start -->\nx\n<!-- a:end -->\nHAND\n<!-- a:end -->\n<!-- b:start -->\n<!-- b:end -->\n"
    assert "end" in _errors(text)


def test_end_before_start_rejected():
    text = "<!-- a:end -->\n<!-- a:start -->\n<!-- b:start -->\n<!-- b:end -->\n"
    assert "before" in _errors(text)


def test_nested_blocks_rejected():
    text = "<!-- a:start -->\n<!-- b:start -->\n<!-- b:end -->\n<!-- a:end -->\n"
    assert "overlap" in _errors(text)


def test_overlapping_blocks_rejected():
    text = "<!-- a:start -->\n<!-- b:start -->\n<!-- a:end -->\n<!-- b:end -->\n"
    assert "overlap" in _errors(text)


def test_missing_marker_rejected():
    text = "<!-- a:start -->\nx\n<!-- a:end -->\n<!-- b:start -->\n"
    msg = _errors(text)
    assert "b" in msg and "end" in msg


def test_all_problems_reported_one_per_line():
    msg = _errors("nothing here\n")
    assert len(msg.splitlines()) >= 4


def test_main_bad_file_untouched(tmp_path, monkeypatch, capsys):
    _fake_blocks(monkeypatch)
    bad = "<!-- a:start -->\nHAND\n<!-- a:start -->\nx\n<!-- a:end -->\n<!-- b:start -->\n<!-- b:end -->\n"
    path = tmp_path / "README.md"
    path.write_text(bad, encoding="utf-8")
    assert script.main(path) == 1
    assert path.read_text(encoding="utf-8") == bad
    assert "a" in capsys.readouterr().err


def test_main_good_file_rewritten(tmp_path, monkeypatch):
    _fake_blocks(monkeypatch)
    path = tmp_path / "README.md"
    path.write_text(GOOD, encoding="utf-8")
    assert script.main(path) == 0
    assert path.read_text(encoding="utf-8") == script.rewrite(GOOD, BLOCKS)
    assert "NEW-A" in path.read_text(encoding="utf-8")


def test_real_readme_validates():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = {name: "" for name in script.helpdoc.README_BLOCKS}
    script.rewrite(text, blocks)
