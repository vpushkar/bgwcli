"""--out is validated before the sweep or fixture capture starts walking the router, and an empty
--out is a usage error wherever --out is accepted."""

from __future__ import annotations

import pytest
from test_cli import fake, run  # noqa: F401  (fake is a fixture)

OUT_COMMANDS = [
    ["sweep", "--pages", "diag", "--delay", "0"],
    ["scan", "--pages", "diag", "--delay", "0"],
    ["schema", "--pages", "diag", "--delay", "0"],
    ["dump"],
    ["fixtures-capture", "--pages", "diag"],
]


@pytest.mark.parametrize("argv", OUT_COMMANDS, ids=lambda argv: argv[0])
@pytest.mark.parametrize("empty", ["", "   "], ids=["empty", "blank"])
def test_an_empty_out_is_a_usage_error(capsys, fake, tmp_path, monkeypatch, argv, empty):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, [*argv, "--out", empty])
    assert code == 1 and out == ""
    assert "--out" in err
    for leftover in ("router-html", "parsed", "sweep.json", "tests"):
        assert not (tmp_path / leftover).exists()
    assert list(tmp_path.glob("*.json")) == []
    assert "client" not in fake or fake["client"].gets == []


@pytest.mark.parametrize("argv", [OUT_COMMANDS[0], OUT_COMMANDS[4]], ids=["sweep", "fixtures-capture"])
def test_a_symlinked_output_root_is_refused_before_any_page_is_read(capsys, fake, tmp_path, argv):  # noqa: F811
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    code, _, err = run(capsys, [*argv, "--out", str(alias)])
    assert code == 2 and "symlink" in err
    assert "client" not in fake or fake["client"].gets == []
    assert list(physical.iterdir()) == []


@pytest.mark.parametrize("argv", [OUT_COMMANDS[0], OUT_COMMANDS[4]], ids=["sweep", "fixtures-capture"])
def test_a_symlinked_artifact_directory_is_refused_before_any_page_is_read(capsys, fake, tmp_path, argv):  # noqa: F811
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (out / "router-html").symlink_to(elsewhere, target_is_directory=True)
    code, _, err = run(capsys, [*argv, "--out", str(out)])
    assert code == 2 and "symlink" in err
    assert "client" not in fake or fake["client"].gets == []
    assert list(elsewhere.iterdir()) == []


def test_an_output_root_that_is_a_file_is_refused_before_any_page_is_read(capsys, fake, tmp_path):  # noqa: F811
    target = tmp_path / "file"
    target.write_text("x")
    code, _, err = run(capsys, ["sweep", "--pages", "diag", "--delay", "0", "--out", str(target)])
    assert code == 2 and "not a directory" in err
    assert "client" not in fake or fake["client"].gets == []
