"""Unreadable local session files are a session-coordination fault (exit 2), not a crash (exit 1)."""

from __future__ import annotations

import json
import os

import pytest
from save_helpers import client_with

from bgwcli import cli
from bgwcli.session import router_session_identity, session_paths

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")

ORIGIN = "http://router.local"


def _unreadable(path, body='{"version": 1}'):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0)
    return path


def _run(monkeypatch, capsys, argv):
    client, wire = client_with(lambda request, n: (_ for _ in ()).throw(AssertionError("router must not be contacted")))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out, f"structured JSON error expected on stdout; stderr: {captured.err!r}"
    return code, json.loads(captured.out), wire


def test_unreadable_cooldown_file_exits_2_without_contacting_the_router(tmp_env, monkeypatch, capsys):
    """An unreadable cooldown may be live: the pool backoff cannot be honoured, so no answer. (An
    unreadable cache file of our own is only "nothing cached"; see test_session.py.)"""
    monkeypatch.setenv("BGW_HOST", "router.local")
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    path = _unreadable(session_paths(ORIGIN).cooldown)

    code, output, wire = _run(monkeypatch, capsys, ["page", "services"])

    assert code == 2 and output["ok"] is False and output["exitCode"] == 2
    assert "Cannot read local router session file" in output["error"] and str(path) in output["error"]
    assert wire.requests == []


def test_session_status_with_an_unreadable_cache_file_exits_2(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BGW_HOST", "router.local")
    path = _unreadable(session_paths(router_session_identity("router.local")).cache)

    code = cli.main(["session", "status", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2 and output["ok"] is False
    assert "Cannot read local router session file" in output["error"] and str(path) in output["error"]


def test_clear_cache_with_an_undeletable_session_file_exits_2(tmp_env, monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.setenv("BGW_HOST", "router.local")
    paths = session_paths(router_session_identity("router.local"))
    paths.cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.cache.write_text('{"version": 1}', encoding="utf-8")
    original_unlink = Path.unlink

    def denied_unlink(self, *args, **kwargs):
        if self == paths.cache:
            raise PermissionError(1, "Operation not permitted")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    code = cli.main(["session", "clear-cache", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 2 and output["ok"] is False
    assert output["errorType"] == "SessionLockError"
    assert "Cannot remove local router session file" in output["error"] and str(paths.cache) in output["error"]


@pytest.mark.parametrize("problem", ["unsearchable", "not-a-directory"])
def test_session_file_behind_an_inaccessible_directory_names_the_directory(tmp_env, monkeypatch, problem):
    """When the cache directory cannot be searched (or is not a directory), the file was never
    reached: the message names the directory to fix, not a session file that does not exist."""
    from bgwcli.errors import SessionLockError
    from bgwcli.session import read_session_state

    directory = tmp_env / "cache"
    if problem == "unsearchable":
        directory.mkdir(mode=0o700)
        directory.chmod(0)
    else:
        directory.write_text("not a directory", encoding="utf-8")
    try:
        with pytest.raises(SessionLockError) as caught:
            read_session_state(ORIGIN)
    finally:
        if problem == "unsearchable":
            directory.chmod(0o700)
    message = str(caught.value)
    assert message.startswith(f"Cannot access local router session directory {directory}:")
    assert ".session.json" not in message


def _foreign_lstat(monkeypatch, target):
    """Report `target` as owned by another user (metadata only: no privileged chown needed)."""
    from pathlib import Path

    actual_lstat = Path.lstat

    def foreign_lstat(path):
        result = actual_lstat(path)
        if path == target:
            data = list(result)
            data[4] = os.geteuid() + 1
            return os.stat_result(data)
        return result

    monkeypatch.setattr(Path, "lstat", foreign_lstat)


@pytest.mark.parametrize("which", ["cache", "cooldown"])
def test_readable_foreign_owned_session_file_is_a_fault_and_is_left_alone(tmp_env, monkeypatch, which):
    from bgwcli import session
    from bgwcli.errors import SessionLockError

    client, _wire = client_with(lambda *_args: pytest.fail("router must not be contacted"))
    client.clear_session()
    paths = session.session_paths(client.session_identity())
    paths.cache.parent.mkdir(mode=0o700, parents=True)
    record = (
        {"origin": client.session_identity(), "authenticated": True, "cookies": {"sid": "foreign-owner"},
         "expiresAt": 9999999999999}
        if which == "cache"
        else {"version": 1, "origin": client.session_identity(), "until": 1, "waitedMs": 0, "retryCount": 0}
    )
    target = getattr(paths, which)
    target.write_text(json.dumps(record))
    _foreign_lstat(monkeypatch, target)
    options = session.SessionCoordinatorOptions(120000, 300000, 1000, False)
    with pytest.raises(SessionLockError, match="another user"):
        session.with_router_session(client, options, lambda: pytest.fail("callback must not run"))
    assert client.has_authenticated_session() is False
    assert json.loads(target.read_text()) == record


def test_foreign_owned_cache_exits_2_from_a_command(tmp_env, monkeypatch, capsys):
    monkeypatch.setenv("BGW_HOST", "router.local")
    monkeypatch.setenv("BGW_ACCESS_CODE", "12345")
    paths = session_paths(ORIGIN)
    paths.cache.parent.mkdir(mode=0o700, parents=True)
    paths.cache.write_text(json.dumps({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "x"},
                                       "expiresAt": 9999999999999}))
    _foreign_lstat(monkeypatch, paths.cache)
    code, output, wire = _run(monkeypatch, capsys, ["page", "services"])
    assert code == 2 and output["exitCode"] == 2 and "another user" in output["error"]
    assert wire.calls == []
