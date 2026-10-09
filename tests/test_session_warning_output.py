"""Failing stderr must not change session outcomes: warnings about ancillary cache failures are best-effort."""

import contextlib
import errno
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from bgwcli import cli, session
from bgwcli.client import BGW320Client, session_pool_full_error

ORIGIN = "https://router.invalid"
OPTIONS = session.SessionCoordinatorOptions(120000, 300000, 1000, False)


class OnceFailingStderr(io.StringIO):
    """First write raises EAGAIN (a non-blocking log destination that is momentarily full); later writes succeed."""

    def __init__(self):
        super().__init__()
        self.write_count = 0

    def write(self, value):
        self.write_count += 1
        if self.write_count == 1:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return super().write(value)


@contextlib.contextmanager
def broken_stderr_pipe():
    """A real OS pipe whose reader has gone away, so every write raises BrokenPipeError."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    stream = io.TextIOWrapper(os.fdopen(write_fd, "wb", buffering=0), write_through=True)
    try:
        with contextlib.redirect_stderr(stream):
            yield stream
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def authenticated_client():
    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("no network expected"))
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "DO-NOT-LOG-COOKIE"}})
    return client


def test_broken_stderr_keeps_successful_result_when_cache_vanishes(tmp_env):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    result = {"committed": True}

    def callback():
        shutil.rmtree(paths.cache.parent)
        return result

    with broken_stderr_pipe():
        returned = session.with_router_session(client, OPTIONS, callback)
    assert returned is result
    assert not paths.lock.exists()


def test_broken_stderr_keeps_returned_pool_full_result_when_cache_vanishes(tmp_env):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    result = {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7}

    def callback():
        shutil.rmtree(paths.cache.parent)
        return result

    with broken_stderr_pipe():
        returned = session.with_router_session(client, OPTIONS, callback)
    assert returned is result
    assert not client.has_authenticated_session()
    assert not paths.lock.exists()


def test_broken_stderr_keeps_raised_pool_full_identity_when_cache_vanishes(tmp_env):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    original = session_pool_full_error(waited_ms=4321, retry_count=7)

    def callback():
        shutil.rmtree(paths.cache.parent)
        raise original

    with broken_stderr_pipe(), pytest.raises(session.RouterSessionPoolFullError) as caught:
        session.with_router_session(client, OPTIONS, callback)
    assert caught.value is original
    assert caught.value.waited_ms == 4321 and caught.value.retry_count == 7
    assert not client.has_authenticated_session()
    assert not paths.lock.exists()


def test_cache_unlink_failure_plus_transient_stderr_failure_still_publishes_cooldown(tmp_env, monkeypatch):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    paths.cache.parent.mkdir(mode=0o700)
    paths.cache.write_text(json.dumps({"origin": ORIGIN, "authenticated": True, "cookies": {}, "version": 1}))
    original_unlink = Path.unlink

    def deny_cache_unlink(path, *args, **kwargs):
        if path == paths.cache:
            raise PermissionError(errno.EACCES, "synthetic cache delete denied", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_cache_unlink)
    original = session_pool_full_error(waited_ms=4321, retry_count=7)
    stderr = OnceFailingStderr()
    with contextlib.redirect_stderr(stderr), pytest.raises(session.RouterSessionPoolFullError) as caught:
        session.with_router_session(client, OPTIONS, lambda: (_ for _ in ()).throw(original))
    assert caught.value is original
    cooldown = json.loads(paths.cooldown.read_text())
    assert cooldown["waitedMs"] == 4321 and cooldown["retryCount"] == 7
    assert stderr.write_count == 1  # the only warning was the unlink one; the cooldown write succeeded
    assert "DO-NOT-LOG-COOKIE" not in stderr.getvalue()


@pytest.mark.parametrize("outcome", ["success", "negative"])
def test_cli_json_and_exit_status_survive_failed_cache_persist_and_failed_warning(tmp_env, monkeypatch, outcome):
    client = authenticated_client()
    result = {"committed": True, "verified": outcome == "success"}
    expected_exit = {"success": 0, "negative": 1}[outcome]

    def completed_command(client, command):
        command.exit_code = expected_exit
        command.output(result, lambda: pytest.fail("expected JSON mode"))
        return result

    def fail_write(path, value):
        raise OSError(errno.ENOSPC, "cache volume is full")

    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    monkeypatch.setattr(cli, "run_command", completed_command)
    monkeypatch.setattr(session, "_write_json", fail_write)
    stdout, stderr = io.StringIO(), OnceFailingStderr()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(["status", "--host", ORIGIN, "--json"])
    assert code == expected_exit
    assert json.loads(stdout.getvalue()) == result
    assert "DO-NOT-LOG-COOKIE" not in stdout.getvalue() + stderr.getvalue()


def test_cli_raised_pool_full_keeps_exit_2_and_metadata_when_cooldown_persist_and_warning_fail(tmp_env, monkeypatch):
    client = authenticated_client()
    original = session_pool_full_error(waited_ms=4321, retry_count=7)

    def pool_full_command(client, command):
        raise original

    def fail_write(path, value):
        raise OSError(errno.ENOSPC, "cache volume is full")

    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    monkeypatch.setattr(cli, "run_command", pool_full_command)
    monkeypatch.setattr(session, "_write_json", fail_write)
    stdout, stderr = io.StringIO(), OnceFailingStderr()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(["status", "--host", ORIGIN, "--json"])
    assert code == 2
    payload = json.loads(stdout.getvalue())
    assert payload["sessionPoolFull"] is True
    assert payload["waitedMs"] == 4321 and payload["retryCount"] == 7
    assert not client.has_authenticated_session()
    assert "DO-NOT-LOG-COOKIE" not in stdout.getvalue() + stderr.getvalue()


def test_warning_still_reaches_a_healthy_redirected_stderr(tmp_env):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()

    def callback():
        shutil.rmtree(paths.cache.parent)
        return {"ok": True}

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        session.with_router_session(client, OPTIONS, callback)
    assert stderr.getvalue().startswith("warning: Could not persist local router session")
    assert "outcome preserved" in stderr.getvalue()
