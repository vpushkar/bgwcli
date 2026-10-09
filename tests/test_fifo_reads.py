"""A user-writable path that is a FIFO is refused without ever blocking: every read-only open of such a
path is non-blocking and no-follow, and the file type and owner are checked before a byte is read."""

from __future__ import annotations

import os
import signal

import pytest

from bgwcli import session
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.snapshot import Snapshot, SnapshotMeta

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")


class _Hang(Exception):
    pass


@pytest.fixture
def no_block():
    """Fail the test (instead of hanging the suite) when an open or read blocks."""

    def alarm(*_):
        raise _Hang("blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def _checkpoint(tmp_path):
    dump = Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}})
    return RecoveryCheckpoint("router.local", dump, None, root=tmp_path / "recovery")


def test_a_fifo_recovery_record_is_refused_not_waited_on(tmp_path, no_block):
    store = _checkpoint(tmp_path)
    store.begin()
    store.path.unlink()
    os.mkfifo(store.path, 0o600)
    with pytest.raises(OSError, match="not a regular file"):
        store.is_active()


def test_a_fifo_session_cache_record_is_not_waited_on(tmp_env, no_block):
    paths = session.session_paths("http://router.local")
    session._ensure_private_dir(paths.cache.parent)
    os.mkfifo(paths.cache, 0o600)
    assert session._read_json(paths.cache) is None


def _record_open_flags(monkeypatch):
    seen: list[tuple[str, int]] = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        seen.append((str(path), flags))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    return seen


def test_every_read_only_open_of_a_user_writable_path_is_nonblocking_and_nofollow(tmp_env, tmp_path, monkeypatch):
    store = _checkpoint(tmp_path)
    store.begin()
    paths = session.session_paths("http://router.local")
    session._ensure_private_dir(paths.cache.parent)
    paths.cache.write_text("{}")
    paths.cache.chmod(0o600)
    paths.lock.write_text("{}")
    paths.lock.chmod(0o600)
    seen = _record_open_flags(monkeypatch)
    store.is_active()
    session._read_json(paths.cache)
    session._remove_stale_lock(paths.lock, 0.0)  # whatever it decides, only the open flags matter here
    reads = [(path, flags) for path, flags in seen if not flags & (os.O_WRONLY | os.O_RDWR | os.O_DIRECTORY)]
    assert reads, "the reads were observed"
    for path, flags in reads:
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW, path


def test_a_non_regular_or_foreign_record_is_a_fault_before_any_read(tmp_path, monkeypatch):
    store = _checkpoint(tmp_path)
    store.begin()
    info = store.path.stat()
    monkeypatch.setattr(os, "geteuid", lambda: info.st_uid + 1)
    with pytest.raises(PermissionError, match="another user"):
        store.is_active()
