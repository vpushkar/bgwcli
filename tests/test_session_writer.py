"""Session persistence rechecks the prepared leaf without preparing its ancestors."""

import errno
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from bgwcli import session
from bgwcli.client import BGW320Client, session_pool_full_error

ORIGIN = "https://router.invalid"
OPTIONS = session.SessionCoordinatorOptions(120000, 300000, 1000, False)


def authenticated_client():
    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("no router request expected"))
    client.import_session({"origin": ORIGIN, "authenticated": True, "cookies": {"sid": "synthetic"}})
    return client


@pytest.mark.parametrize("outcome", ["authenticated", "returned-pool-full", "raised-pool-full"])
def test_persistence_uses_prepared_leaf_without_repeating_ancestor_setup(tmp_env, monkeypatch, outcome):
    client = authenticated_client()
    paths = session.session_paths(ORIGIN)
    pool_error = session_pool_full_error(waited_ms=4321, retry_count=7)
    result = {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7}

    def reject_preparation(*args, **kwargs):
        pytest.fail("persistence must not prepare directories again")

    def callback():
        assert stat.S_IMODE(paths.cache.parent.stat().st_mode) == 0o700
        assert paths.cache.parent.stat().st_uid == os.geteuid()
        assert paths.lock.exists()
        monkeypatch.setattr(session, "_ensure_private_dir", reject_preparation)
        monkeypatch.setattr(session.filesystem, "ensure_directory", reject_preparation)
        monkeypatch.setattr(Path, "mkdir", reject_preparation)
        if outcome == "raised-pool-full":
            raise pool_error
        return result if outcome == "returned-pool-full" else "completed"

    if outcome == "raised-pool-full":
        with pytest.raises(session.RouterSessionPoolFullError) as caught:
            session.with_router_session(client, OPTIONS, callback)
        assert caught.value is pool_error
    else:
        returned = session.with_router_session(client, OPTIONS, callback)
        assert returned is result if outcome == "returned-pool-full" else returned == "completed"
    published = paths.cache if outcome == "authenticated" else paths.cooldown
    record = json.loads(published.read_text())
    assert record["origin"] == ORIGIN
    if outcome == "authenticated":
        assert record["cookies"] == {"sid": "synthetic"}
    else:
        assert record["waitedMs"] == 4321 and record["retryCount"] == 7
        assert not paths.cache.exists()
        assert not client.has_authenticated_session()
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert not paths.lock.exists()
    assert not list(paths.cache.parent.glob("*.tmp"))


def test_direct_writer_does_not_create_missing_parent(tmp_path):
    parent = tmp_path / "missing" / "cache"
    with pytest.raises(session.SessionLockError) as caught:
        session._write_json(parent / "state.json", {"version": 1})
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert str(parent) in str(caught.value)
    assert not (tmp_path / "missing").exists()


def test_direct_writer_replaces_destination_symlink_without_directory_setup(tmp_path, monkeypatch):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("preserve target")
    path = parent / "state.json"
    path.symlink_to(target)

    def reject_preparation(*args, **kwargs):
        pytest.fail("direct writer requires an already prepared directory")

    monkeypatch.setattr(session, "_ensure_private_dir", reject_preparation)
    monkeypatch.setattr(Path, "mkdir", reject_preparation)
    session._write_json(path, {"version": 1, "value": "synthetic"})
    assert path.read_text() == '{"version":1,"value":"synthetic"}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.is_symlink()
    assert target.read_text() == "preserve target"
    assert not list(parent.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["mode", "symlink", "file", "foreign-owner"])
def test_persistence_rechecks_leaf_changed_by_callback(tmp_env, monkeypatch, capsys, kind):
    paths = session.session_paths(ORIGIN)
    leaf = paths.cache.parent
    target = tmp_env / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve")
    original_lstat = Path.lstat

    def callback():
        if kind == "mode":
            leaf.chmod(0o755)
        elif kind in {"symlink", "file"}:
            leaf.rename(tmp_env / "displaced-cache")
            if kind == "symlink":
                leaf.symlink_to(target, target_is_directory=True)
            else:
                leaf.write_text("preserve file")
        else:

            def foreign_leaf(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if path == leaf:
                    fields = list(info)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return info

            monkeypatch.setattr(Path, "lstat", foreign_leaf)
        return "completed"

    if kind == "mode":
        assert session.with_router_session(authenticated_client(), OPTIONS, callback) == "completed"
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
        assert paths.cache.exists()
        assert not paths.lock.exists()
    else:
        assert session.with_router_session(authenticated_client(), OPTIONS, callback) == "completed"
        captured = capsys.readouterr()
        assert "Cannot prepare session cache directory" in captured.err
        assert "command outcome preserved" in captured.err
        if kind == "file":
            assert leaf.read_text() == "preserve file"
        else:
            assert not paths.cache.exists()
            assert not list(leaf.glob("*.tmp"))
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("relative", [False, True])
def test_direct_writer_refuses_root_before_permission_changes(monkeypatch, relative):
    monkeypatch.chdir("/")
    path = Path("state.json") if relative else Path("/state.json")

    def forbid_mutation(*args, **kwargs):
        pytest.fail("writer must reject filesystem root before any mutation")

    monkeypatch.setattr(Path, "chmod", forbid_mutation)
    monkeypatch.setattr(os, "chmod", forbid_mutation)
    monkeypatch.setattr(os, "open", forbid_mutation)
    with pytest.raises(session.SessionLockError, match="Session cache directory.*filesystem root") as caught:
        session._write_json(path, {"version": 1})
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize("configuration", ["current-cache", "relative-cache", "relative-xdg"])
def test_relative_cache_stays_at_entry_directory_when_callback_changes_cwd(tmp_env, monkeypatch, configuration):
    initial = tmp_env / "initial"
    initial.mkdir()
    destination = tmp_env / "destination"
    destination.mkdir()
    monkeypatch.chdir(initial)
    if configuration == "current-cache":
        monkeypatch.setenv("BGW_SESSION_CACHE_DIR", ".")
        expected = initial
    elif configuration == "relative-cache":
        monkeypatch.setenv("BGW_SESSION_CACHE_DIR", "relative/cache")
        expected = initial / "relative" / "cache"
    else:
        monkeypatch.delenv("BGW_SESSION_CACHE_DIR")
        monkeypatch.setenv("XDG_CACHE_HOME", "relative")
        expected = initial / "relative" / "bgw"

    def callback():
        monkeypatch.chdir(destination)
        return "completed"

    assert session.with_router_session(authenticated_client(), OPTIONS, callback) == "completed"
    records = list(expected.glob("*.session.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["cookies"] == {"sid": "synthetic"}
    assert not list(expected.glob("*.lock"))
    assert list(destination.iterdir()) == []


def test_writer_publication_error_preserves_original_and_removes_temporary(tmp_path, monkeypatch):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    path = parent / "state.json"
    path.write_text("original record")
    failure = OSError("synthetic replace failure")

    def fail_publication(*args, **kwargs):
        raise failure

    monkeypatch.setattr(os, "replace", fail_publication)
    with pytest.raises(OSError) as caught:
        session._write_json(path, {"version": 1})
    assert caught.value is failure
    assert path.read_text() == "original record"
    assert list(parent.iterdir()) == [path]


@pytest.mark.parametrize("replacement", ["directory", "symlink", "file", "foreign-owner"])
def test_release_preserves_replaced_parent_and_closes_lifetime_descriptor(tmp_path, monkeypatch, replacement):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    path = parent / "router.lock"
    descriptors = []
    original_publish = session._publish_lock
    original_lstat = Path.lstat

    def observe_publish(*args, **kwargs):
        descriptor = original_publish(*args, **kwargs)
        if descriptor is not None:
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(session, "_publish_lock", observe_publish)
    release = session._acquire_lock(path, 1000)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve target")
    if replacement == "foreign-owner":

        def foreign_parent(directory, *args, **kwargs):
            info = original_lstat(directory, *args, **kwargs)
            if directory == parent:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", foreign_parent)
        original_record = path.read_text()
    else:
        parent.rename(tmp_path / "displaced-cache")
        if replacement == "directory":
            parent.mkdir(mode=0o700)
            path.write_text("replacement owner")
        elif replacement == "symlink":
            parent.symlink_to(target, target_is_directory=True)
        else:
            parent.write_text("preserve file")
    release()
    release()  # Closing the lifetime descriptor remains idempotent.
    assert len(descriptors) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(descriptors[0])
    assert caught.value.errno == errno.EBADF
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve target"
    if replacement == "directory":
        assert list(parent.iterdir()) == [path]
        assert path.read_text() == "replacement owner"
    elif replacement == "file":
        assert parent.read_text() == "preserve file"
    elif replacement == "foreign-owner":
        assert path.read_text() == original_record


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_replaced_cache_cleanup_preserves_callback_outcome_and_allows_reclaim(tmp_env, outcome):
    paths = session.session_paths(ORIGIN)
    target = tmp_env / "target"
    target.mkdir(mode=0o755)
    displaced = tmp_env / "displaced-cache"
    result = object()
    error = RuntimeError("synthetic callback failure")
    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("no router request expected"))

    def callback():
        paths.cache.parent.rename(displaced)
        paths.cache.parent.symlink_to(target, target_is_directory=True)
        if outcome == "error":
            raise error
        return result

    if outcome == "error":
        with pytest.raises(RuntimeError) as caught:
            session.with_router_session(client, OPTIONS, callback)
        assert caught.value is error
    else:
        assert session.with_router_session(client, OPTIONS, callback) is result
    assert list(target.iterdir()) == []
    # The displaced marker must be reclaimable immediately: cleanup closed its lifetime flock.
    marker = displaced / paths.lock.name
    assert marker.exists()
    release = session._acquire_lock(marker, 0)
    release()
    assert not marker.exists()


@pytest.mark.parametrize("outcome", ["returned-pool-full", "raised-pool-full"])
def test_pool_full_rejects_swapped_leaf_before_deleting_target_cache(tmp_env, capsys, outcome):
    paths = session.session_paths(ORIGIN)
    target = tmp_env / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    records = {
        target / paths.cache.name: json.dumps(
            {
                "origin": ORIGIN,
                "authenticated": True,
                "cookies": {"sid": "preserve-session"},
                "version": 1,
                "cachedAt": 1,
                "expiresAt": 2,
            }
        ),
        target / paths.cooldown.name: '{"version":1,"until":123}',
        target / (paths.lock.name + ".guard"): "preserve guard",
        target / "existing.tmp": "preserve temporary",
    }
    for path, content in records.items():
        path.write_text(content)
    before = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_ino) for path in records}
    displaced = tmp_env / "displaced-cache"
    pool_error = session_pool_full_error(waited_ms=4321, retry_count=7)
    result = {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7}
    client = authenticated_client()

    def callback():
        paths.cache.parent.rename(displaced)
        paths.cache.parent.symlink_to(target, target_is_directory=True)
        if outcome == "raised-pool-full":
            raise pool_error
        return result

    if outcome == "raised-pool-full":
        with pytest.raises(session.RouterSessionPoolFullError) as caught:
            session.with_router_session(client, OPTIONS, callback)
        assert caught.value is pool_error
    else:
        assert session.with_router_session(client, OPTIONS, callback) is result
    assert not client.has_authenticated_session()
    captured = capsys.readouterr()
    assert "Cannot prepare session cache directory" in captured.err
    assert "pool-full outcome preserved" in captured.err
    assert paths.cache.parent.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert set(target.iterdir()) == set(records)
    assert {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_ino) for path in records} == before
    # A replaced parent skips pathname cleanup but must release its lifetime flock.
    marker = displaced / paths.lock.name
    assert marker.exists()
    release = session._acquire_lock(marker, 0)
    release()
    assert not marker.exists()


@pytest.mark.parametrize("outcome", ["authenticated", "returned-pool-full", "raised-pool-full"])
def test_removed_cache_does_not_replace_completed_outcome(tmp_env, capsys, outcome):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    result = (
        {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7} if outcome == "returned-pool-full" else object()
    )
    error = session_pool_full_error(waited_ms=4321, retry_count=7)

    def callback():
        shutil.rmtree(paths.cache.parent)
        if outcome == "raised-pool-full":
            raise error
        return result

    if outcome == "raised-pool-full":
        with pytest.raises(session.RouterSessionPoolFullError) as caught:
            session.with_router_session(client, OPTIONS, callback)
        assert caught.value is error
    else:
        assert session.with_router_session(client, OPTIONS, callback) is result
    assert not paths.cache.parent.exists()  # Do not create a new lock namespace mid-operation.
    captured = capsys.readouterr()
    assert str(paths.cache.parent) in captured.err
    assert "outcome preserved" in captured.err
    if outcome != "authenticated":
        assert not client.has_authenticated_session()


@pytest.mark.parametrize("outcome", ["authenticated", "returned-pool-full", "raised-pool-full"])
@pytest.mark.parametrize("failure", ["write", "unlink"])
def test_cache_io_failure_preserves_completed_outcome(tmp_env, monkeypatch, capsys, outcome, failure):
    paths = session.session_paths(ORIGIN)
    client = authenticated_client()
    result = (
        {"sessionPoolFull": True, "waitedMs": 4321, "retryCount": 7} if outcome == "returned-pool-full" else object()
    )
    error = session_pool_full_error(waited_ms=4321, retry_count=7)
    original_fsync = os.fsync
    original_unlink = Path.unlink
    paths.cache.parent.mkdir(mode=0o700)
    paths.cache.write_text(json.dumps({
        "origin": ORIGIN, "authenticated": True, "cookies": {"sid": "old-session"},
        "version": 1, "expiresAt": session._now_ms() + 60000,
    }))

    def fail_file_sync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.ENOSPC, "synthetic cache disk full")
        return original_fsync(descriptor)

    def fail_record_delete(path, *args, **kwargs):
        if path == (paths.cooldown if outcome == "authenticated" else paths.cache):
            raise PermissionError(errno.EACCES, "synthetic cache delete denied", str(path))
        return original_unlink(path, *args, **kwargs)

    def callback():
        if failure == "write":
            monkeypatch.setattr(os, "fsync", fail_file_sync)
        else:
            monkeypatch.setattr(Path, "unlink", fail_record_delete)
        if outcome == "raised-pool-full":
            raise error
        # The run used the session (an authenticated answer), so a successful run persists it.
        client.authenticated_requests += 1
        return result

    if outcome == "raised-pool-full":
        with pytest.raises(session.RouterSessionPoolFullError) as caught:
            session.with_router_session(client, OPTIONS, callback)
        assert caught.value is error
    else:
        assert session.with_router_session(client, OPTIONS, callback) is result
    captured = capsys.readouterr()
    assert "outcome preserved" in captured.err
    assert "synthetic cache" in captured.err
    assert not paths.lock.exists()
    assert not list(paths.cache.parent.glob("*.tmp"))
    if outcome != "authenticated":
        assert not client.has_authenticated_session()
        if failure == "unlink":
            assert json.loads(paths.cooldown.read_text())["retryCount"] == 7
        else:
            assert not paths.cache.exists()  # A failed cooldown write must not leave stale login cookies.


def test_short_writes_still_publish_the_complete_cooldown_record(tmp_env, monkeypatch):
    """os.write may write fewer bytes than asked: the writer keeps going until the whole record is out,
    so the next command still sees the live cooldown instead of an unparsable (ignored) file."""
    client = authenticated_client()
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data[:20]))

    monkeypatch.setattr(os, "write", short_write)

    def pool_full():
        raise session_pool_full_error(waited_ms=123, retry_count=4)

    with pytest.raises(session.RouterSessionPoolFullError):
        session.with_router_session(client, OPTIONS, pool_full)
    paths = session.session_paths(client.session_identity())
    record = json.loads(paths.cooldown.read_text())
    assert record["waitedMs"] == 123 and record["retryCount"] == 4
    with pytest.raises(session.RouterSessionPoolFullError, match="cooldown is active"):
        session.with_router_session(client, OPTIONS, lambda: pytest.fail("cooldown must block the callback"))


def test_a_write_without_progress_fails_and_keeps_the_prior_record(tmp_path, monkeypatch):
    parent = tmp_path / "cache"
    parent.mkdir(mode=0o700)
    path = parent / "state.json"
    path.write_text("original record")
    real_write = os.write
    calls = []

    def stalled_write(fd, data):
        calls.append(len(data))
        return real_write(fd, bytes(data[:5])) if len(calls) == 1 else 0

    monkeypatch.setattr(os, "write", stalled_write)
    with pytest.raises(OSError):
        session._write_json(path, {"version": 1, "origin": "x"})
    assert path.read_text() == "original record"
    assert list(parent.iterdir()) == [path]
