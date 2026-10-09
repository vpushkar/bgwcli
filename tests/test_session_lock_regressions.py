"""Offline process and cleanup regressions for router session lock ownership."""

import errno
import fcntl
import json
import multiprocessing
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from bgwcli import session
from bgwcli.client import BGW320Client, session_pool_full_error
from bgwcli.errors import BgwError, RouterAuthError
from bgwcli.exit_codes import fatal_exit_code

ORIGIN = "http://router.invalid"


def _crashing_owner(path, ready):
    _release = session._acquire_lock(Path(path), 1000)
    ready.set()
    os._exit(0)  # Neither Python finalizers nor _release() run.


@pytest.mark.parametrize("pid_state", ["reused", "foreign", "uncertain"])
def test_crashed_owner_reclaimable_despite_reused_or_uncertain_pid(tmp_path, monkeypatch, pid_state):
    path = tmp_path / "router.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=_crashing_owner, args=(str(path), ready))
    child.start()
    try:
        assert ready.wait(5)
        child.join(5)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
    owner = json.loads(path.read_text())
    owner["pid"] = os.getpid()  # PID reused by an unrelated, still-live process.
    path.write_text(json.dumps(owner))
    old = time.time() - 901
    os.utime(path, (old, old))
    if pid_state != "reused":

        def inaccessible_pid(pid, signal):
            raise PermissionError("foreign process") if pid_state == "foreign" else OSError("uncertain process")

        monkeypatch.setattr(session.os, "kill", inaccessible_pid)
    release = session._acquire_lock(path, 0)
    release()
    assert not path.exists()


@pytest.mark.parametrize("pid_state", ["alive", "foreign", "uncertain"])
def test_legacy_marker_with_unproven_dead_owner_is_preserved(tmp_path, monkeypatch, pid_state):
    path = tmp_path / "router.lock"
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    path.write_text(original)
    old = time.time() - 901
    os.utime(path, (old, old))
    if pid_state != "alive":

        def inaccessible_pid(pid, signal):
            raise PermissionError("foreign process") if pid_state == "foreign" else OSError("uncertain process")

        monkeypatch.setattr(session.os, "kill", inaccessible_pid)
    with pytest.raises(session.SessionLockTimeoutError):
        session._acquire_lock(path, 0)
    assert path.read_text() == original


@pytest.mark.parametrize("outcome", ["success", "auth", "pool", "other"])
def test_release_contention_preserves_result_and_error_and_allows_reclaim(tmp_env, outcome):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    client = BGW320Client(ORIGIN, transport=lambda request: pytest.fail("no network expected"))
    opts = session.SessionCoordinatorOptions(120000, 300000, 300000, False)
    error = {
        "auth": RouterAuthError("original authentication failure"),
        "pool": session_pool_full_error("original pool failure"),
        "other": ValueError("original operation failure"),
    }.get(outcome)

    with guard.open("w") as held:

        def run():
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if error is not None:
                raise error
            return "original result"

        started = time.monotonic()
        if error is None:
            assert session.with_router_session(client, opts, run) == "original result"
        else:
            with pytest.raises(type(error)) as caught:
                session.with_router_session(client, opts, run)
            assert caught.value is error
        assert 0.8 <= time.monotonic() - started < 3
        assert paths.lock.exists()  # Cleanup deferred after its short independent budget.
    # The abandoned marker is young and names this live process, but no longer owns a lock.
    release = session._acquire_lock(paths.lock, 0)
    release()
    assert not paths.lock.exists()


def test_clear_cleanup_contention_preserves_original_error(tmp_env, monkeypatch):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    original_unlink = Path.unlink
    error = PermissionError("original cache deletion failure")

    with guard.open("w") as held:

        def fail_cache_delete(path, *args, **kwargs):
            if path == paths.cache:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise error
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_cache_delete)
        # The deletion failure is reported as a session-coordination fault (exit 2) with the original
        # error as its cause; the release failure under contention must not mask it.
        with pytest.raises(session.SessionLockError) as caught:
            session.clear_session_state(ORIGIN, lock_timeout_ms=0)
        assert caught.value.__cause__ is error
        assert str(paths.cache) in str(caught.value)
    release = session._acquire_lock(paths.lock, 0)
    release()
    assert not paths.lock.exists()


def test_release_filesystem_failure_is_non_masking_and_reclaimable(tmp_env, monkeypatch):
    paths = session.session_paths(ORIGIN)
    original_open = os.open
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    client = BGW320Client(ORIGIN, transport=lambda request: pytest.fail("no network expected"))
    opts = session.SessionCoordinatorOptions(120000, 300000, 0, False)

    def fail_guard_open(path, *args, **kwargs):
        if path == guard:
            raise PermissionError("cleanup guard unavailable")
        return original_open(path, *args, **kwargs)

    def run():
        monkeypatch.setattr(session.os, "open", fail_guard_open)
        return "original result"

    assert session.with_router_session(client, opts, run) == "original result"
    monkeypatch.setattr(session.os, "open", original_open)
    release = session._acquire_lock(paths.lock, 0)
    release()
    assert not paths.lock.exists()


def test_cooldown_cleanup_contention_preserves_original_pool_error(tmp_env, monkeypatch):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    until = session._now_ms() + 60000
    paths.cooldown.write_text(json.dumps({"until": until, "waitedMs": 17, "retryCount": 3}))
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    original_apply_cooldown = session._apply_cooldown
    client = BGW320Client(ORIGIN, transport=lambda request: pytest.fail("no network expected"))
    opts = session.SessionCoordinatorOptions(120000, 300000, 0, False)
    with guard.open("w") as held:

        def contend_during_cooldown(path, *args):
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_apply_cooldown(path, *args)

        monkeypatch.setattr(session, "_apply_cooldown", contend_during_cooldown)
        with pytest.raises(session.RouterSessionPoolFullError) as caught:
            session.with_router_session(client, opts, lambda: pytest.fail("cooldown must refuse the run"))
        assert caught.value.waited_ms == 17
        assert caught.value.retry_count == 3
        assert json.loads(paths.cooldown.read_text())["until"] == until
    release = session._acquire_lock(paths.lock, 0)
    release()
    assert not paths.lock.exists()


def _contending_owner(path, start, active, overlap, entered):
    assert start.wait(5)
    release = session._acquire_lock(Path(path), 5000)
    try:
        with active.get_lock():
            active.value += 1
            if active.value != 1:
                overlap.value = 1
        with entered.get_lock():
            entered.value += 1
        # Other reclaimers must respect the lifetime flock even with an ancient marker.
        old = time.time() - 901
        os.utime(path, (old, old))
        time.sleep(0.05)
        with active.get_lock():
            active.value -= 1
    finally:
        release()


def test_competing_reclaimers_serialize_after_protocol_owner_crashes(tmp_path):
    path = tmp_path / "router.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=_crashing_owner, args=(str(path), ready))
    child.start()
    try:
        assert ready.wait(5)
        child.join(5)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
    # Keep a live PID in the orphaned marker, as happens after PID reuse.
    owner = json.loads(path.read_text())
    owner["pid"] = os.getpid()
    path.write_text(json.dumps(owner))
    start = context.Event()
    active, overlap, entered = (context.Value("i", 0) for _ in range(3))
    workers = [
        context.Process(target=_contending_owner, args=(str(path), start, active, overlap, entered)) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    start.set()
    try:
        for worker in workers:
            worker.join(10)
        assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
        assert entered.value == 4
        assert overlap.value == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)
    # A release contending with a reclaimer may leave a harmless marker behind.
    release = session._acquire_lock(path, 1000)
    release()
    assert not path.exists()


@pytest.mark.parametrize("old", [False, True])
@pytest.mark.parametrize("protocol", [False, True])
def test_unreadable_marker_preserved_with_actionable_error(tmp_path, old, protocol):
    path = tmp_path / "router.lock"
    original = json.dumps({"pid": os.getpid(), "ownership": "flock-v1" if protocol else "legacy"})
    path.write_text(original)
    if old:
        os.utime(path, (time.time() - 901,) * 2)
    inode = path.stat().st_ino
    path.chmod(0)
    try:
        started = time.monotonic()
        # A stale unreadable marker is a lock fault (exit 2); a young one is still contention (exit 1).
        with pytest.raises(session.SessionLockError) as caught:
            session._acquire_lock(path, 150)
        assert isinstance(caught.value, session.SessionLockTimeoutError) is not old
        assert fatal_exit_code(caught.value) == (2 if old else 1)
        assert str(path) in str(caught.value)
        assert isinstance(caught.value.__cause__, PermissionError)
        elapsed = time.monotonic() - started
        assert elapsed < 0.12 if old else 0.12 <= elapsed < 2
        assert path.stat().st_ino == inode
    finally:
        path.chmod(0o600)
    assert path.read_text() == original


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("inspection", ["open", "read"])
def test_permission_denied_during_marker_inspection_is_conservative(tmp_path, monkeypatch, error_number, inspection):
    path = tmp_path / "router.lock"
    original = json.dumps({"pid": os.getpid(), "ownership": "flock-v1"})
    path.write_text(original)
    inode = path.stat().st_ino
    original_open = os.open
    original_read = Path.read_text

    def denied_open(candidate, flags, *args, **kwargs):
        if candidate == path and not flags & os.O_CREAT:
            raise PermissionError(error_number, "marker ownership unavailable")
        return original_open(candidate, flags, *args, **kwargs)

    def denied_read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError(error_number, "marker ownership unavailable")
        return original_read(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        if inspection == "open":
            patch.setattr(session.os, "open", denied_open)
        else:
            patch.setattr(Path, "read_text", denied_read)
        with pytest.raises(session.SessionLockTimeoutError):
            session._acquire_lock(path, 0)
    assert path.stat().st_ino == inode
    assert path.read_text() == original


def _brief_guard_holder(path, ready, finish):
    with open(path, "w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        ready.set()
        assert finish.wait(5)
        time.sleep(0.2)


def test_release_removes_marker_after_brief_process_guard_contention(tmp_path):
    path = tmp_path / "router.lock"
    release = session._acquire_lock(path, 0)
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(target=_brief_guard_holder, args=(str(path) + ".guard", ready, finish))
    child.start()
    try:
        assert ready.wait(5)
        finish.set()
        release()
        assert not path.exists()
        child.join(5)
        assert child.exitcode == 0
    finally:
        finish.set()
        if child.is_alive():
            child.terminate()
        child.join(5)
        release()


@pytest.mark.parametrize("protocol", [False, True])
def test_abandoned_marker_unlink_permission_fails_promptly(tmp_path, monkeypatch, protocol):
    path = tmp_path / "router.lock"
    original = json.dumps({"pid": 87654321, "ownership": "flock-v1" if protocol else "legacy"})
    path.write_text(original)
    os.utime(path, (time.time() - 901,) * 2)
    original_unlink = Path.unlink
    denied = PermissionError(errno.EACCES, "unlink denied")

    def unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise denied
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("abandoned lock cannot benefit from waiting"))
    with pytest.raises(session.SessionLockError) as caught:
        session._acquire_lock(path, 300000)
    assert not isinstance(caught.value, session.SessionLockTimeoutError)
    assert fatal_exit_code(caught.value) == 2
    assert str(path) in str(caught.value) and "abandoned" in str(caught.value)
    assert caught.value.__cause__ is denied
    assert path.read_text() == original


@pytest.mark.parametrize("content", [
    "", "invalid", '{}', '{"pid": null}', '{"pid": false}', '{"ownership": "flock-v1"}',
])
def test_stale_unlocked_ownerless_reserved_marker_is_reclaimed(tmp_path, content):
    path = tmp_path / "router.lock"
    path.write_text(content)
    os.utime(path, (time.time() - 901,) * 2)
    release = session._acquire_lock(path, 0)
    release()
    assert not path.exists()


@pytest.mark.parametrize("content", [
    "", "invalid", '{}', '{"pid": null}', '{"pid": false}', '{"ownership": "flock-v1"}',
])
@pytest.mark.parametrize("held", [False, True])
def test_fresh_or_flock_held_ownerless_marker_is_preserved(tmp_path, content, held):
    path = tmp_path / "router.lock"
    path.write_text(content)
    with path.open("r") as stream:
        if held:
            os.utime(path, (time.time() - 901,) * 2)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(session.SessionLockTimeoutError):
            session._acquire_lock(path, 0)
        assert path.read_text() == content


def _crash_during_lock_write(path, partial):
    original_write = os.write

    def crash(descriptor, content):
        if partial:
            original_write(descriptor, content[:4])
        os._exit(73)

    os.write = crash
    session._acquire_lock(Path(path), 0)


@pytest.mark.parametrize("partial", [False, True])
def test_crash_during_ownership_write_never_publishes_partial_marker(tmp_path, partial):
    path = tmp_path / "router.lock"
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crash_during_lock_write, args=(str(path), partial))
    child.start()
    child.join(5)
    try:
        assert child.exitcode == 73
        assert not path.exists()
        release = session._acquire_lock(path, 0)
        release()
        assert not path.exists()
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)


def test_short_ownership_writes_publish_complete_record(tmp_path, monkeypatch):
    path = tmp_path / "router.lock"
    original_write = os.write
    monkeypatch.setattr(session.os, "write", lambda fd, content: original_write(fd, content[:4]))
    release = session._acquire_lock(path, 0)
    try:
        assert json.loads(path.read_text())["ownership"] == "flock-v1"
    finally:
        release()
    assert not path.exists()


def test_lock_publication_and_release_under_restrictive_umask(tmp_path):
    path = tmp_path / "router.lock"
    previous = os.umask(0o777)
    try:
        release = session._acquire_lock(path, 0)
        try:
            assert path.stat().st_mode & 0o777 == 0o600
            assert json.loads(path.read_text())["ownership"] == "flock-v1"
        finally:
            release()
        assert not path.exists()
    finally:
        os.umask(previous)


def test_reclamation_preserves_replacement_inode(tmp_path, monkeypatch):
    path = tmp_path / "router.lock"
    path.write_text("")
    os.utime(path, (time.time() - 901,) * 2)
    replacement = tmp_path / "replacement"
    replacement.write_text("replacement owner")
    original_flock = fcntl.flock

    def flock(descriptor, operation):
        result = original_flock(descriptor, operation)
        if os.fstat(descriptor).st_ino == path.stat().st_ino:
            replacement.replace(path)
        return result

    monkeypatch.setattr(session.fcntl, "flock", flock)
    with pytest.raises(session.SessionLockTimeoutError):
        session._acquire_lock(path, 0)
    assert path.read_text() == "replacement owner"


@pytest.mark.parametrize("failure", ["zero", "error"])
def test_failed_ownership_write_leaves_no_reserved_marker(tmp_path, monkeypatch, failure):
    path = tmp_path / "router.lock"

    def write(descriptor, content):
        if failure == "error":
            raise OSError("injected partial ownership failure")
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(session.os, "write", write)
        with pytest.raises(session.SessionLockError) as caught:
            session._acquire_lock(path, 0)
    assert fatal_exit_code(caught.value) == 2
    assert isinstance(caught.value.__cause__, OSError)
    assert not path.exists()
    assert not list(tmp_path.glob(".bgw-lock-*"))
    release = session._acquire_lock(path, 0)
    release()
    assert not path.exists()


@pytest.mark.parametrize("kind", ["foreign-owner", "hardlink", "symlink"])
@pytest.mark.parametrize("content", ["", '{"pid": true}', '{"pid": 1.0}'])
def test_stale_ownerless_foreign_or_shared_marker_is_preserved(tmp_path, monkeypatch, kind, content):
    path = tmp_path / "router.lock"
    target = tmp_path / "target"
    target.write_text(content)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        path.hardlink_to(target)
    else:
        path.write_text(content)
        actual_uid = os.geteuid()
        monkeypatch.setattr(session.os, "geteuid", lambda: actual_uid + 1)
    os.utime(path, (time.time() - 901,) * 2)
    inode = path.lstat().st_ino
    with pytest.raises(session.SessionLockTimeoutError):
        session._acquire_lock(path, 0)
    assert path.lstat().st_ino == inode
    assert path.read_bytes() == target.read_bytes() == content.encode()


@pytest.mark.parametrize("stale", [False, True])
def test_invalid_utf8_owner_record_obeys_stale_boundary(tmp_path, stale):
    path = tmp_path / "router.lock"
    path.write_bytes(b"\xff")
    if stale:
        os.utime(path, (time.time() - 901,) * 2)
        release = session._acquire_lock(path, 0)
        release()
        assert not path.exists()
    else:
        with pytest.raises(session.SessionLockTimeoutError):
            session._acquire_lock(path, 0)
        assert path.read_bytes() == b"\xff"


@pytest.mark.parametrize("error_number, unsupported", [
    (errno.EPERM, False), (errno.ENOSPC, False), (errno.EIO, False),
    (errno.EMLINK, False), (errno.ENOENT, False),
    pytest.param(errno.ENOTSUP, True, id="ENOTSUP"),
    pytest.param(errno.EOPNOTSUPP, True, id="EOPNOTSUPP"),
    (errno.ENOSYS, True),
])
@pytest.mark.parametrize("existing", [False, True])
def test_hardlink_publication_failure_is_actionable_and_cleans_up(
    tmp_path, monkeypatch, error_number, unsupported, existing,
):
    path = tmp_path / "router.lock"
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    if existing:
        path.write_text(original)
    descriptors = []
    original_mkstemp = session.tempfile.mkstemp
    error = OSError(error_number, "injected publication failure")

    def mkstemp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, name

    def failed_link(*args, **kwargs):
        raise error

    monkeypatch.setattr(session.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(session.os, "link", failed_link)
    monkeypatch.setattr(session, "_sleep", lambda _: pytest.fail("publication failure must fail promptly"))
    with pytest.raises(BgwError) as caught:
        session._acquire_lock(path, 300000)
    message = str(caught.value)
    assert str(path) in message
    assert "hard link" in message
    assert str(error) in message
    assert ("BGW_SESSION_CACHE_DIR" in message) is (unsupported or error_number == errno.EPERM)
    assert ("unsupported" in message.lower()) is unsupported
    if error_number == errno.EPERM:
        assert "permissions" in message.lower()
        assert "if" in message.lower()
    assert caught.value.__cause__ is error
    assert not list(tmp_path.glob(".bgw-lock-*"))
    assert len(descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
    if existing:
        assert path.read_text() == original
    else:
        assert not path.exists()


@pytest.mark.parametrize("kind", ["foreign-owner", "hardlink"])
def test_stale_legacy_proven_dead_pid_is_reclaimed_without_ownerless_gate(tmp_path, monkeypatch, kind):
    path = tmp_path / "router.lock"
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=time.sleep, args=(0,))
    child.start()
    child.join(5)
    try:
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
    original = json.dumps({"pid": child.pid, "createdAt": 0})
    path.write_text(original)
    os.utime(path, (time.time() - 901,) * 2)
    alias = tmp_path / "legacy-alias"
    if kind == "foreign-owner":
        actual_uid = os.geteuid()
        monkeypatch.setattr(session.os, "geteuid", lambda: actual_uid + 1)
    else:
        alias.hardlink_to(path)
    release = session._acquire_lock(path, 0)
    release()
    assert not path.exists()
    if kind == "hardlink":
        assert alias.read_text() == original


@pytest.mark.parametrize("pid", [None, False, True, 0, -1, 1.0, "1", 2**100])
def test_invalid_or_uninspectable_pid_cannot_prove_death(pid):
    assert session._pid_is_alive(pid) is True


@pytest.mark.parametrize("site", ["guard", "publication", "inspection"])
@pytest.mark.parametrize("error_number", [
    errno.ENOLCK, pytest.param(errno.ENOTSUP, id="ENOTSUP"),
    pytest.param(errno.EOPNOTSUPP, id="EOPNOTSUPP"), errno.EIO,
])
def test_flock_failure_reports_actual_cause_and_releases_resources(tmp_env, monkeypatch, site, error_number):
    paths = session.session_paths(ORIGIN)
    paths.lock.parent.mkdir()
    guard = paths.lock.with_name(paths.lock.name + ".guard")
    original = json.dumps({"pid": os.getpid(), "createdAt": 0})
    if site != "publication":
        paths.lock.write_text(original)
        inode = paths.lock.stat().st_ino
    error = OSError(error_number, "injected flock failure")
    failed_descriptors = []
    original_flock = fcntl.flock

    def failing_flock(descriptor, operation):
        opened = os.fstat(descriptor)
        if guard.exists() and guard.stat().st_ino == opened.st_ino:
            current_site = "guard"
        elif paths.lock.exists() and paths.lock.stat().st_ino == opened.st_ino:
            current_site = "inspection"
        else:
            current_site = "publication"
        if current_site == site:
            failed_descriptors.append(descriptor)
            raise error
        return original_flock(descriptor, operation)

    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("no router request expected"))
    opts = session.SessionCoordinatorOptions(120000, 300000, 300000, False)
    ran = []
    with monkeypatch.context() as patch:
        patch.setattr(session.fcntl, "flock", failing_flock)
        patch.setattr(session, "_sleep", lambda _: pytest.fail("flock fault must fail promptly"))
        with pytest.raises(BgwError) as caught:
            session.with_router_session(client, opts, lambda: ran.append(True))
    assert not ran
    message = str(caught.value)
    assert str(guard if site == "guard" else paths.lock) in message
    assert "flock" in message
    assert str(error) in message
    unsupported = error_number in (errno.ENOTSUP, errno.EOPNOTSUPP)
    assert ("BGW_SESSION_CACHE_DIR" in message) is unsupported
    assert ("unsupported" in message.lower()) is unsupported
    if error_number == errno.ENOLCK:
        assert "resources" in message.lower()
    assert caught.value.__cause__ is error
    assert len(failed_descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(failed_descriptors[0])
    assert closed.value.errno == errno.EBADF
    assert not list(paths.lock.parent.glob(".bgw-lock-*"))
    with guard.open("r") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if site != "publication":
        assert paths.lock.stat().st_ino == inode
        assert paths.lock.read_text() == original
    else:
        assert not paths.lock.exists()


@pytest.mark.parametrize("error_number", [errno.ENOLCK, errno.EOPNOTSUPP, errno.EIO])
@pytest.mark.parametrize("outcome", ["success", "error"])
def test_cleanup_flock_fault_preserves_original_operation(tmp_env, monkeypatch, error_number, outcome):
    client = BGW320Client(ORIGIN, transport=lambda _: pytest.fail("no router request expected"))
    opts = session.SessionCoordinatorOptions(120000, 300000, 0, False)
    original = ValueError("original callback failure")

    def failed_flock(*args):
        raise OSError(error_number, "cleanup flock failure")

    with monkeypatch.context() as patch:

        def run():
            patch.setattr(session.fcntl, "flock", failed_flock)
            if outcome == "error":
                raise original
            return "original result"

        if outcome == "error":
            with pytest.raises(ValueError) as caught:
                session.with_router_session(client, opts, run)
            assert caught.value is original
        else:
            assert session.with_router_session(client, opts, run) == "original result"
    path = session.session_paths(ORIGIN).lock
    assert path.exists()
    release = session._acquire_lock(path, 0)
    release()
    assert not path.exists()


@pytest.mark.parametrize("site", ["guard", "publication", "inspection"])
def test_interrupted_flock_is_not_reclassified(tmp_path, monkeypatch, site):
    path = tmp_path / "router.lock"
    if site == "inspection":
        path.write_text(json.dumps({"pid": os.getpid(), "createdAt": 0}))
    error = InterruptedError(errno.EINTR, "interrupted flock")

    def interrupted(*args):
        raise error

    monkeypatch.setattr(session.fcntl, "flock", interrupted)
    with pytest.raises(InterruptedError) as caught:
        if site == "guard":
            with session._lock_guard(path, time.monotonic()):
                pytest.fail("guard must not be acquired")
        elif site == "publication":
            session._publish_lock(path, {"pid": os.getpid()})
        else:
            session._remove_stale_lock(path, 900000)
    assert caught.value is error
    assert not list(tmp_path.glob(".bgw-lock-*"))


@pytest.mark.parametrize("old", [False, True])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_inspection_flock_permission_failure_retains_stale_policy(tmp_path, monkeypatch, old, error_number):
    path = tmp_path / "router.lock"
    path.write_text(json.dumps({"pid": os.getpid(), "createdAt": 0}))
    if old:
        os.utime(path, (time.time() - 901,) * 2)
    inode = path.stat().st_ino
    error = PermissionError(error_number, "marker flock denied")
    original_flock = fcntl.flock

    def denied(descriptor, operation):
        if os.fstat(descriptor).st_ino == inode:
            raise error
        return original_flock(descriptor, operation)

    monkeypatch.setattr(session.fcntl, "flock", denied)
    with pytest.raises(session.SessionLockError) as caught:
        session._acquire_lock(path, 0)
    assert isinstance(caught.value, session.SessionLockTimeoutError) is not old
    assert fatal_exit_code(caught.value) == (2 if old else 1)
    assert caught.value.__cause__ is error
    assert str(path) in str(caught.value)
    assert path.stat().st_ino == inode


def test_unopenable_lock_guard_is_a_session_lock_error(tmp_path):
    """A guard that cannot be opened is a lock infrastructure fault (exit 2) like every other lock
    failure, not a raw OSError that fatal_exit_code maps to the exit 1 negative answer."""
    from bgwcli.errors import SessionLockError
    from bgwcli.exit_codes import fatal_exit_code

    path = tmp_path / "router.lock"
    guard = tmp_path / "router.lock.guard"
    guard.symlink_to(tmp_path / "elsewhere")  # O_NOFOLLOW refuses it with ELOOP

    with pytest.raises(SessionLockError) as caught:
        session._acquire_lock(path, 1000)

    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.ELOOP
    assert "guard" in str(caught.value) and str(path) in str(caught.value)
    assert fatal_exit_code(caught.value) == 2
    assert not path.exists()


def _acquire_lock_promptly(path):
    """Acquire with a short deadline: succeeds only if no leaked descriptor still holds the flock."""
    release = session._acquire_lock(path, 300)
    release()


def test_fstat_failure_after_publication_closes_the_owner_descriptor(tmp_path, monkeypatch):
    """_publish_lock hands back a descriptor that already holds the lifetime flock. If the fstat that
    follows fails, that descriptor must be closed on the way out, or the lock stays held with no
    release closure anywhere and every later acquisition times out."""
    path = tmp_path / "router.lock"
    published = []
    original_publish = session._publish_lock

    def recording_publish(lock_path, owner):
        fd = original_publish(lock_path, owner)
        published.append(fd)
        return fd

    original_fstat = os.fstat

    def failing_fstat(fd):
        if published and fd == published[-1]:
            raise OSError(errno.EIO, "offline injected inspection-read")
        return original_fstat(fd)

    monkeypatch.setattr(session, "_publish_lock", recording_publish)
    monkeypatch.setattr(session.os, "fstat", failing_fstat)
    with pytest.raises(session.SessionLockError) as caught:
        session._acquire_lock(path, 1000)
    assert fatal_exit_code(caught.value) == 2
    assert caught.value.__cause__.errno == errno.EIO
    assert str(path) in str(caught.value)
    monkeypatch.setattr(session.os, "fstat", original_fstat)

    assert published and published[-1] is not None
    with pytest.raises(OSError):  # the leaked-looking number is closed: EBADF
        os.fstat(published[-1])
    _acquire_lock_promptly(path)  # the complete marker is reclaimable, no lingering flock


def test_guard_exit_failure_after_publication_closes_the_owner_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "router.lock"
    published = []
    original_publish = session._publish_lock
    original_guard = session._lock_guard

    def recording_publish(lock_path, owner):
        fd = original_publish(lock_path, owner)
        published.append(fd)
        return fd

    @contextmanager
    def guard_that_fails_on_exit(lock_path, deadline):
        with original_guard(lock_path, deadline):
            yield
        raise OSError(errno.EIO, "offline injected guard close failure")

    monkeypatch.setattr(session, "_publish_lock", recording_publish)
    monkeypatch.setattr(session, "_lock_guard", guard_that_fails_on_exit)
    with pytest.raises(OSError) as caught:
        session._acquire_lock(path, 1000)
    assert caught.value.errno == errno.EIO
    monkeypatch.setattr(session, "_lock_guard", original_guard)

    assert published and published[-1] is not None
    with pytest.raises(OSError):
        os.fstat(published[-1])
    _acquire_lock_promptly(path)
