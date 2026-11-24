"""dump, diff, restore and autorestore select pages with --include and refuse --pages.

Review finding (2026-11-24): `restore BASE --pages services --prune --commit --confirm RESTORE` parsed,
ignored --pages and restored the whole dump. --pages stays the sweep family's traversal limit.
"""

import json

import pytest
from test_cli import fake, run  # noqa: F401

from bgwcli import cli


@pytest.mark.parametrize("name", ["restore", "autorestore", "diff", "dump"])
def test_pages_is_refused_where_include_selects_pages(capsys, fake, tmp_path, name):  # noqa: F811
    argv = [name] + ([] if name == "dump" else [str(tmp_path / "dump.json")]) + ["--pages", "services"]
    if name in ("restore", "autorestore"):
        argv += ["--prune"] if name == "restore" else []
        argv += ["--commit", "--confirm", "RESTORE"]
    code, out, err = run(capsys, argv)
    assert code == 1, (out, err)
    assert "--pages" in err and "--include" in err
    assert "client" not in fake or (fake["client"].gets == [] and fake["client"].posts == [])


def test_pages_still_works_for_sweep_commands():
    command = cli.parse_args(["sweep", "--pages", "home"])
    assert command.pages == ["home"]
    assert json.dumps(command.pages)
