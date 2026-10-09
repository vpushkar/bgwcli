"""Unsafe cache paths prevent command execution with fatal CLI diagnostics."""

import json
import os
import shutil

import pytest

from bgwcli import cli, session
from bgwcli.client import BGW320Client, session_pool_full_error


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("kind", ["self-loop", "parent-loop", "symlink", "foreign-owner", "file"])
def test_public_cli_unsafe_cache_is_fatal_and_preserves_entries(tmp_env, monkeypatch, capsys, command, kind):
    leaf = tmp_env / "cache"
    target = tmp_env / "target"
    target.mkdir()
    target.chmod(0o755)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve")
    if kind == "parent-loop":
        parent = tmp_env / "parent"
        parent.symlink_to(parent, target_is_directory=True)
        leaf = parent / "cache"
        entry = parent
    else:
        entry = leaf
        if kind == "self-loop":
            leaf.symlink_to(leaf, target_is_directory=True)
        elif kind == "symlink":
            leaf.symlink_to(target, target_is_directory=True)
        elif kind == "file":
            leaf.write_text("preserve")
        else:
            leaf.mkdir()
            leaf.chmod(0o755)
            uid = os.geteuid()
            monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    original = entry.lstat()
    original_target = target.stat()
    before_names = sorted(p.name for p in tmp_env.iterdir())
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    monkeypatch.setenv("BGW_ACCESS_CODE", "DO-NOT-PRINT-THIS-CREDENTIAL")
    client = BGW320Client(
        "https://router.invalid", transport=lambda _: pytest.fail("unsafe cache must prevent router requests"),
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    errors = []
    original_mapper = cli.fatal_exit_code

    def capture_error(error):
        errors.append(error)
        return original_mapper(error)

    monkeypatch.setattr(cli, "fatal_exit_code", capture_error)
    assert cli.main(command + ["--host", "https://router.invalid"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "Cannot prepare session cache directory" in captured.err
    assert str(leaf) in captured.err
    assert "DO-NOT-PRINT-THIS-CREDENTIAL" not in captured.err
    assert len(errors) == 1
    assert isinstance(errors[0], session.SessionLockError)
    assert isinstance(errors[0].__cause__, OSError)
    assert entry.lstat().st_ino == original.st_ino
    assert entry.lstat().st_mode == original.st_mode
    assert target.stat().st_mode == original_target.st_mode
    assert sorted(p.name for p in tmp_env.iterdir()) == before_names
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve"
    if kind == "foreign-owner":
        assert list(leaf.iterdir()) == []
    elif kind == "file":
        assert leaf.read_text() == "preserve"


@pytest.mark.parametrize("outcome", ["success", "negative", "pool-full", "returned-pool-full"])
def test_removed_cache_preserves_cli_json_and_exit_status(tmp_env, monkeypatch, capsys, outcome):
    origin = "https://router.invalid"
    paths = session.session_paths(origin)
    client = BGW320Client(origin, transport=lambda _: pytest.fail("no network expected"))
    client.import_session({"origin": origin, "authenticated": True, "cookies": {"sid": "DO-NOT-LOG-COOKIE"}})
    result = (
        {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7} if outcome == "returned-pool-full"
        else {"committed": True, "verified": outcome == "success"}
    )

    def completed_command(client, command):
        shutil.rmtree(paths.cache.parent)
        if outcome == "pool-full":
            raise session_pool_full_error(waited_ms=4321, retry_count=7)
        command.exit_code = {"success": 0, "negative": 1, "returned-pool-full": 2}[outcome]
        command.output(result, lambda: pytest.fail("expected JSON mode"))
        return result

    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    monkeypatch.setattr(cli, "run_command", completed_command)
    assert cli.main(["status", "--host", origin, "--json"]) == {
        "success": 0, "negative": 1, "pool-full": 2, "returned-pool-full": 2,
    }[outcome]
    captured = capsys.readouterr()
    actual = json.loads(captured.out)
    if outcome in {"pool-full", "returned-pool-full"}:
        assert actual["sessionPoolFull"] is True
        assert actual["waitedMs"] == 4321
        assert actual["retryCount"] == 7
        assert not client.has_authenticated_session()
    else:
        assert actual == result
    assert captured.err.startswith("warning: ")
    assert "outcome preserved" in captured.err
    assert "DO-NOT-LOG-COOKIE" not in captured.out + captured.err
    assert not paths.cache.parent.exists()
