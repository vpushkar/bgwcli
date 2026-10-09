"""Offline public CLI regressions for infrastructure faults and lock contention."""

import errno
import fcntl
import json
import os

import pytest

from bgwcli import cli, errors, exit_codes, session
from bgwcli.client import BGW320Client

ORIGIN = "https://router.invalid"


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
def test_parent_removed_before_lock_acquisition_has_cache_diagnostic(tmp_env, monkeypatch, capsys, command):
    paths = session.session_paths(ORIGIN)
    original_prepare = session._ensure_private_dir
    errors_seen = []
    original_mapper = cli.fatal_exit_code

    def prepare_then_remove(directory):
        original_prepare(directory)
        directory.rmdir()

    def record_error(error):
        errors_seen.append(error)
        return original_mapper(error)

    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("missing cache must prevent router requests"))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    monkeypatch.setattr(session, "_ensure_private_dir", prepare_then_remove)
    monkeypatch.setattr(cli, "fatal_exit_code", record_error)
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "Cannot prepare session cache directory" in captured.err
    assert str(paths.cache.parent) in captured.err
    assert len(errors_seen) == 1
    assert isinstance(errors_seen[0], session.SessionLockError)
    assert isinstance(errors_seen[0].__cause__, FileNotFoundError)


@pytest.fixture
def offline_client(monkeypatch, tmp_env):
    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("lock acquisition must precede router requests"))
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    return client


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("site,error_number", [
    ("guard", errno.ENOLCK), ("guard", errno.EIO), ("guard", errno.EOPNOTSUPP),
    ("publication", errno.ENOLCK), ("publication", errno.EIO), ("publication", errno.EOPNOTSUPP),
    ("inspection", errno.ENOLCK), ("inspection", errno.EIO), ("inspection", errno.EOPNOTSUPP),
    ("hardlink", errno.EPERM), ("hardlink", errno.ENOSPC),
    ("hardlink", errno.EIO), ("hardlink", errno.EOPNOTSUPP),
])
def test_public_cli_lock_infrastructure_fault_is_exit_two(
    offline_client, monkeypatch, capsys, command, site, error_number,
):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    if site == "inspection":
        paths.lock.write_text(original)
        inode = paths.lock.stat().st_ino
    error = OSError(error_number, "injected lock infrastructure failure")
    failed_descriptors = []
    original_flock = fcntl.flock

    def failing_flock(descriptor, operation):
        opened = os.fstat(descriptor)
        current_site = (
            "guard" if guard.exists() and guard.stat().st_ino == opened.st_ino else
            "inspection" if paths.lock.exists() and paths.lock.stat().st_ino == opened.st_ino else
            "publication"
        )
        if current_site == site:
            failed_descriptors.append(descriptor)
            raise error
        return original_flock(descriptor, operation)

    def failing_link(*args, **kwargs):
        raise error

    monkeypatch.setattr(session.fcntl, "flock", failing_flock)
    if site == "hardlink":
        monkeypatch.setattr(session.os, "link", failing_link)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("infrastructure failure must not wait as contention"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert str(error) in captured.err
    assert str(guard if site == "guard" else paths.lock) in captured.err
    assert ("hard link" if site == "hardlink" else "flock") in captured.err
    assert not list(paths.lock.parent.glob(".bgw-lock-*"))
    for descriptor in failed_descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    with guard.open("r") as stream:
        original_flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if site == "inspection":
        assert paths.lock.stat().st_ino == inode
        assert paths.lock.read_text() == original
    else:
        assert not paths.lock.exists()


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("site", ["guard", "marker"])
def test_public_cli_held_lock_timeout_stays_exit_one(offline_client, monkeypatch, capsys, command, site):
    monkeypatch.setenv("BGW_SESSION_LOCK_TIMEOUT_MS", "1")
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    held_path = paths.lock.with_name(paths.lock.name + ".guard") if site == "guard" else paths.lock
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    held_path.write_text(original)
    inode = held_path.stat().st_ino
    with held_path.open("r") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(command + ["--host", ORIGIN]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Timed out waiting for local router session lock" in captured.err
    assert held_path.stat().st_ino == inode
    assert held_path.read_text() == original


def test_shared_lock_error_family_preserves_exit_mapping():
    operation_error = session.SessionLockError("unavailable lock infrastructure")
    timeout = session.SessionLockTimeoutError("held lock timed out")
    assert exit_codes.fatal_exit_code(operation_error) == 2
    assert exit_codes.fatal_exit_code(timeout) == 1
    assert isinstance(timeout, session.SessionLockError)
    assert getattr(errors, "SessionLockError", None) is session.SessionLockError
    assert getattr(errors, "SessionLockTimeoutError", None) is session.SessionLockTimeoutError


@pytest.mark.parametrize("site", ["guard", "publication"])
def test_flock_eperm_retains_permissions_cause_without_hardlink_hint(tmp_path, monkeypatch, site):
    path = tmp_path / "router.lock"
    error = PermissionError(errno.EPERM, "injected flock policy denial")

    def denied(*args):
        raise error

    monkeypatch.setattr(session.fcntl, "flock", denied)
    with pytest.raises(session.SessionLockError) as caught:
        if site == "guard":
            with session._lock_guard(path, 0):
                pytest.fail("denied guard must not be acquired")
        else:
            session._publish_lock(path, {"pid": os.getpid()})
    assert caught.value.__cause__ is error
    assert str(error) in str(caught.value)
    assert "BGW_SESSION_CACHE_DIR" not in str(caught.value)
    assert "unsupported" not in str(caught.value).lower()
    assert not list(tmp_path.glob(".bgw-lock-*"))


def _stale_marker(paths, content):
    paths.lock.parent.mkdir()
    paths.lock.write_text(content)
    os.utime(paths.lock, (os.path.getmtime(paths.lock) - 901,) * 2)
    return paths.lock.stat().st_ino


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
def test_public_cli_stale_marker_unlink_permission_fault_is_exit_two(offline_client, monkeypatch, capsys, command):
    from pathlib import Path

    paths = session.session_paths(ORIGIN)
    original = json.dumps({"pid": 87654321, "ownership": "legacy"})
    _stale_marker(paths, original)
    original_unlink = Path.unlink
    denied = PermissionError(errno.EACCES, "unlink denied")

    def unlink(candidate, *args, **kwargs):
        if candidate == paths.lock:
            raise denied
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("abandoned lock cannot benefit from waiting"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "abandoned" in captured.err and str(paths.lock) in captured.err and "unlink denied" in captured.err
    assert paths.lock.read_text() == original


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("inspection", ["open", "flock"])
def test_public_cli_unreadable_stale_marker_is_exit_two(offline_client, monkeypatch, capsys, command, inspection):
    paths = session.session_paths(ORIGIN)
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    inode = _stale_marker(paths, original)
    denied = PermissionError(errno.EACCES, "marker ownership unavailable")
    original_open, original_flock = os.open, fcntl.flock

    def denied_open(candidate, flags, *args, **kwargs):
        if str(candidate) == str(paths.lock) and not flags & os.O_CREAT:
            raise denied
        return original_open(candidate, flags, *args, **kwargs)

    def denied_flock(descriptor, operation):
        if os.fstat(descriptor).st_ino == inode:
            raise denied
        return original_flock(descriptor, operation)

    if inspection == "open":
        monkeypatch.setattr(session.os, "open", denied_open)
    else:
        monkeypatch.setattr(session.fcntl, "flock", denied_flock)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("a stale unreadable marker cannot wait"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "stale" in captured.err and "marker preserved" in captured.err
    assert str(paths.lock) in captured.err and "marker ownership unavailable" in captured.err
    assert paths.lock.stat().st_ino == inode
    assert paths.lock.read_text() == original


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
def test_public_cli_guard_permission_fault_is_exit_two(offline_client, monkeypatch, capsys, command):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    guard.touch()
    denied = PermissionError(errno.EPERM, "guard owned by another user")
    original_fchmod = os.fchmod

    def fchmod(descriptor, mode):
        if os.fstat(descriptor).st_ino == guard.stat().st_ino:
            raise denied
        return original_fchmod(descriptor, mode)

    monkeypatch.setattr(session.os, "fchmod", fchmod)
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "guard" in captured.err and str(paths.lock) in captured.err and str(denied) in captured.err


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("site", ["mkstemp", "write"])
def test_public_cli_ownership_record_publication_fault_is_exit_two(offline_client, monkeypatch, capsys, command, site):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    error = OSError(errno.ENOSPC if site == "mkstemp" else errno.EIO, f"injected {site} failure")

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(session.tempfile if site == "mkstemp" else session.os, site, failing)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("filesystem faults must not wait as contention"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert str(error) in captured.err and str(paths.lock) in captured.err
    assert not paths.lock.exists()
    assert not list(paths.lock.parent.glob(".bgw-lock-*"))


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
@pytest.mark.parametrize("site", ["lstat", "open"])
def test_public_cli_stale_inspection_io_fault_is_exit_two(offline_client, monkeypatch, capsys, command, site):
    from pathlib import Path

    paths = session.session_paths(ORIGIN)
    original = json.dumps({"pid": 87654321, "ownership": "legacy"})
    inode = _stale_marker(paths, original)
    error = OSError(errno.EIO, f"injected {site} failure")
    original_lstat, original_open = Path.lstat, os.open

    def failing_lstat(candidate, *args, **kwargs):
        if candidate == paths.lock:
            raise error
        return original_lstat(candidate, *args, **kwargs)

    def failing_open(candidate, flags, *args, **kwargs):
        if str(candidate) == str(paths.lock) and not flags & os.O_CREAT:
            raise error
        return original_open(candidate, flags, *args, **kwargs)

    if site == "lstat":
        monkeypatch.setattr(Path, "lstat", failing_lstat)
    else:
        monkeypatch.setattr(session.os, "open", failing_open)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("filesystem faults must not wait as contention"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert str(error) in captured.err and str(paths.lock) in captured.err
    if site == "lstat":
        monkeypatch.setattr(Path, "lstat", original_lstat)
    assert paths.lock.stat().st_ino == inode
    assert paths.lock.read_text() == original


def _fail_temporary_record_unlink(monkeypatch, paths):
    from pathlib import Path

    original_unlink = Path.unlink
    denied = PermissionError(errno.EACCES, "temporary record unlink denied")

    def unlink(candidate, *args, **kwargs):
        if candidate.parent == paths.lock.parent and candidate.name.startswith(".bgw-lock-"):
            raise denied
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    return denied


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
def test_public_cli_temporary_record_cleanup_fault_is_exit_two(offline_client, monkeypatch, capsys, command):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    denied = _fail_temporary_record_unlink(monkeypatch, paths)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("a cleanup fault must not wait as contention"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert str(denied) in captured.err and str(paths.lock) in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", [["session", "clear-cache"], ["status"]])
def test_temporary_record_cleanup_fault_does_not_mask_the_failure_in_flight(
    offline_client, monkeypatch, capsys, command,
):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    denied = _fail_temporary_record_unlink(monkeypatch, paths)
    write_error = OSError(errno.EIO, "injected write failure")

    def failing_write(*args, **kwargs):
        raise write_error

    monkeypatch.setattr(session.os, "write", failing_write)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("filesystem faults must not wait as contention"))
    assert cli.main(command + ["--host", ORIGIN]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    # The ownership-record failure is the reported error; the cleanup fault is only a warning.
    assert str(write_error) in captured.err and "write the ownership record" in captured.err
    assert "warning:" in captured.err and str(denied) in captured.err
    assert not paths.lock.exists()
