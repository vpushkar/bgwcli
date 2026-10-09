"""`dump --out` refuses an unusable target before any router request, and `preflight_dump_target` says why."""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest
from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, form, html

from bgwcli import cli
from bgwcli.dumpfile import preflight_dump_target, write_dump_file
from bgwcli.snapshot import Snapshot, SnapshotMeta

_SNAPSHOT = Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"setting": "new"}})


def _fail_temporary_open(monkeypatch, before=None):
    real_open = os.open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            if before is not None:
                before(path)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


def test_failed_open_removes_the_directories_this_call_created(tmp_path, monkeypatch):
    target = tmp_path / "new" / "deep" / "dump.json"
    _fail_temporary_open(monkeypatch)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert caught.value.errno == errno.ENOSPC and str(target) in str(caught.value) and ".tmp" not in str(caught.value)
    assert not (tmp_path / "new").exists()
    assert tmp_path.is_dir() and list(tmp_path.iterdir()) == []


def test_open_error_without_an_errno_is_named_truthfully(tmp_path, monkeypatch):
    target = tmp_path / "dump.json"
    real_open = os.open

    def fake_open(path, *args, **kwargs):
        if str(path).endswith(".tmp"):
            raise OSError("disk gone")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    text = str(caught.value)
    assert "disk gone" in text and str(target) in text and ".tmp" not in text
    assert "Errno None" not in text


def test_open_error_with_an_errno_keeps_the_errno_text(tmp_path, monkeypatch):
    target = tmp_path / "dump.json"
    _fail_temporary_open(monkeypatch)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert "[Errno 28]" in str(caught.value)


def test_failed_directory_creation_removes_the_directories_already_created(tmp_path, monkeypatch):
    first = tmp_path / "new"
    second = first / "deep"
    target = second / "dump.json"
    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self == second:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    with pytest.raises(PermissionError):
        write_dump_file(target, _SNAPSHOT)
    assert not first.exists()
    assert tmp_path.is_dir() and list(tmp_path.iterdir()) == []


def test_failed_directory_creation_names_the_target_not_the_component(tmp_path, monkeypatch):
    second = tmp_path / "new" / "deep"
    target = second / "dump.json"
    real_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self == second:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    with pytest.raises(PermissionError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert str(target) in str(caught.value)
    assert str(second) + "'" not in str(caught.value)
    assert not (tmp_path / "new").exists()


def test_failed_chmod_of_a_created_directory_names_the_target_and_cleans_up(tmp_path, monkeypatch):
    first = tmp_path / "new"
    target = first / "deep" / "dump.json"
    real_chmod = os.chmod

    def fake_chmod(path, *args, **kwargs):
        if Path(path) == first:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", fake_chmod)
    with pytest.raises(PermissionError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert str(target) in str(caught.value)
    assert str(first) + "'" not in str(caught.value)
    assert not first.exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_fsync_names_the_target_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "new" / "dump.json"

    def fake_fsync(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", fake_fsync)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert caught.value.errno == errno.ENOSPC and str(target) in str(caught.value) and ".tmp" not in str(caught.value)
    assert not (tmp_path / "new").exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_data_write_names_the_target_and_cleans_up(tmp_path, monkeypatch):
    # The buffered write does not go through os.write, so the fault is raised by the file object.
    target = tmp_path / "new" / "deep" / "dump.json"

    class _FullDisk:
        def __init__(self, fd):
            self._fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            os.close(self._fd)

        def write(self, text):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: _FullDisk(fd))
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert caught.value.errno == errno.ENOSPC and str(target) in str(caught.value) and ".tmp" not in str(caught.value)
    assert not (tmp_path / "new").exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_replace_removes_the_temporary_and_the_created_directories(tmp_path, monkeypatch):
    target = tmp_path / "new" / "deep" / "dump.json"
    real_replace = os.replace

    def fake_replace(src, dst, *args, **kwargs):
        if str(src).endswith(".tmp"):
            assert os.path.exists(src)
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert caught.value.errno == errno.EIO and str(target) in str(caught.value)
    assert not (tmp_path / "new").exists()
    assert list(tmp_path.iterdir()) == []


def test_failed_write_keeps_a_created_directory_that_is_not_empty(tmp_path, monkeypatch):
    target = tmp_path / "new" / "deep" / "dump.json"
    _fail_temporary_open(monkeypatch, before=lambda path: (target.parent / "other.txt").write_text("keep"))
    with pytest.raises(OSError):
        write_dump_file(target, _SNAPSHOT)
    assert (target.parent / "other.txt").read_text() == "keep"
    assert (tmp_path / "new").is_dir()
    assert not list(tmp_path.glob("**/*.tmp"))


def test_failed_write_never_removes_a_pre_existing_directory(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    target = existing / "dump.json"
    _fail_temporary_open(monkeypatch)
    with pytest.raises(OSError):
        write_dump_file(target, _SNAPSHOT)
    assert existing.is_dir() and list(existing.iterdir()) == []


def _another_process_creates_first(monkeypatch, directory):
    """The first mkdir of exactly `directory` finds it already created by someone else (mode 0755)."""
    real_mkdir = Path.mkdir
    raced = []

    def fake_mkdir(self, *args, **kwargs):
        if self == directory and not raced:
            raced.append(self)
            os.mkdir(self, 0o755)
            os.chmod(self, 0o755)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)


def test_failed_write_keeps_a_directory_another_process_created_meanwhile(tmp_path, monkeypatch):
    outer = tmp_path / "new"
    target = outer / "deep" / "dump.json"
    _another_process_creates_first(monkeypatch, outer)
    _fail_temporary_open(monkeypatch)
    with pytest.raises(OSError) as caught:
        write_dump_file(target, _SNAPSHOT)
    assert caught.value.errno == errno.ENOSPC and str(target) in str(caught.value)
    assert outer.is_dir()
    assert not (outer / "deep").exists()


def test_a_directory_another_process_created_meanwhile_keeps_its_mode(tmp_path, monkeypatch):
    outer = tmp_path / "new"
    target = outer / "deep" / "dump.json"
    _another_process_creates_first(monkeypatch, outer)
    write_dump_file(target, _SNAPSHOT)
    assert stat.S_IMODE(outer.stat().st_mode) == 0o755
    assert stat.S_IMODE((outer / "deep").stat().st_mode) == 0o700
    assert target.is_file()


def _dump(monkeypatch, capsys, out, *extra):
    def handler(request, _n):
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["dump", "--out", str(out), *extra])
    captured = capsys.readouterr()
    return code, captured, wire


def _refused(code, captured, wire, shown):
    assert code == 2
    assert "nothing was written" in captured.err and shown in captured.err
    assert ".tmp" not in captured.err
    assert wire.requests == []  # no login, no page read


def test_dump_out_an_existing_directory_is_refused_before_any_request(tmp_env, tmp_path, clock, monkeypatch, capsys):
    work = tmp_path / "work"  # tmp_env keeps its own cache directory directly under tmp_path
    target = work / "backups"
    target.mkdir(parents=True)
    code, captured, wire = _dump(monkeypatch, capsys, target)
    _refused(code, captured, wire, str(target))
    assert list(target.iterdir()) == [] and [p.name for p in work.iterdir()] == ["backups"]


def test_dump_out_through_a_regular_file_is_refused_before_any_request(tmp_env, tmp_path, clock, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    blocker = work / "file.txt"
    blocker.write_text("keep")
    target = blocker / "sub.json"
    code, captured, wire = _dump(monkeypatch, capsys, target)
    _refused(code, captured, wire, str(target))
    assert blocker.read_text() == "keep" and [p.name for p in work.iterdir()] == ["file.txt"]


def test_dump_out_refusal_is_a_structured_error_with_json(tmp_env, tmp_path, clock, monkeypatch, capsys):
    target = tmp_path / "backups"
    target.mkdir()
    code, captured, wire = _dump(monkeypatch, capsys, target, "--json")
    out = json.loads(captured.out)
    assert code == 2 and out["ok"] is False and out["exitCode"] == 2
    assert out["errorType"] == "IsADirectoryError"
    assert str(target) in out["error"] and "nothing was written" in out["error"]
    assert wire.requests == []


def test_dump_out_under_missing_directories_still_writes(tmp_env, tmp_path, clock, monkeypatch, capsys):
    target = tmp_path / "new" / "deep" / "dump.json"
    code, captured, wire = _dump(monkeypatch, capsys, target)
    assert code == 0, captured.err
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "new" / "deep").stat().st_mode) == 0o700
    assert wire.requests  # the page reads ran
    assert not list(tmp_path.glob("**/*.tmp"))


def test_preflight_refuses_a_directory(tmp_path):
    with pytest.raises(IsADirectoryError) as caught:
        preflight_dump_target(tmp_path)
    assert str(tmp_path) in str(caught.value) and "nothing was written" in str(caught.value)


def test_preflight_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(PermissionError) as caught:
        preflight_dump_target(link)
    assert str(link) in str(caught.value) and "nothing was written" in str(caught.value)


def test_preflight_refuses_a_dangling_symlink(tmp_path):
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "missing.json")
    with pytest.raises(PermissionError):
        preflight_dump_target(link)


def test_preflight_refuses_a_path_through_a_dangling_symlink(tmp_path):
    # A stale link (an unmounted backup disk, a removed directory) is a realistic parent: the write
    # could never complete there, so it is refused before any page is read, like a file in the way.
    link = tmp_path / "backups"
    link.symlink_to(tmp_path / "unmounted")
    for target in (link / "dump.json", link / "a" / "b" / "dump.json"):
        with pytest.raises(NotADirectoryError) as caught:
            preflight_dump_target(target)
        assert str(target) in str(caught.value) and "nothing was written" in str(caught.value)
    assert link.is_symlink() and not link.exists() and [p.name for p in tmp_path.iterdir()] == ["backups"]


def test_dump_out_through_a_dangling_symlink_is_refused_early(tmp_env, tmp_path, clock, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    link = work / "backups"
    link.symlink_to(work / "unmounted")
    target = link / "dump.json"
    code, captured, wire = _dump(monkeypatch, capsys, target)
    _refused(code, captured, wire, str(target))
    assert link.is_symlink() and [p.name for p in work.iterdir()] == ["backups"]


def test_preflight_refuses_a_path_through_a_regular_file(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    for target in (blocker / "sub.json", blocker / "a" / "b" / "sub.json"):
        with pytest.raises(NotADirectoryError) as caught:
            preflight_dump_target(target)
        assert str(target) in str(caught.value) and "nothing was written" in str(caught.value)


def test_preflight_refuses_a_file_owned_by_another_user(tmp_path, monkeypatch):
    existing = tmp_path / "dump.json"
    existing.write_text("{}")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(existing).st_uid + 1)
    with pytest.raises(PermissionError) as caught:
        preflight_dump_target(existing)
    assert str(existing) in str(caught.value) and "another user" in str(caught.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_preflight_refuses_a_non_regular_file(tmp_path):
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(PermissionError) as caught:
        preflight_dump_target(fifo)
    assert str(fifo) in str(caught.value) and "nothing was written" in str(caught.value)


needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses directory permissions"
)


@pytest.fixture
def read_only_directory(tmp_path):
    directory = tmp_path / "work" / "ro"
    directory.mkdir(parents=True)
    directory.chmod(0o500)
    yield directory
    directory.chmod(0o700)


@needs_permissions
def test_dump_out_into_an_unwritable_directory_is_refused_before_any_request(
    tmp_env, read_only_directory, clock, monkeypatch, capsys
):
    target = read_only_directory / "d.json"
    code, captured, wire = _dump(monkeypatch, capsys, target)
    _refused(code, captured, wire, str(target))
    assert list(read_only_directory.iterdir()) == []


@needs_permissions
def test_dump_out_under_missing_directories_of_an_unwritable_one_is_refused(
    tmp_env, read_only_directory, clock, monkeypatch, capsys
):
    target = read_only_directory / "new" / "d.json"
    code, captured, wire = _dump(monkeypatch, capsys, target)
    _refused(code, captured, wire, str(target))


@needs_permissions
def test_preflight_refuses_an_unwritable_directory(read_only_directory):
    target = read_only_directory / "d.json"
    with pytest.raises(PermissionError) as caught:
        preflight_dump_target(target)
    assert str(target) in str(caught.value) and "nothing was written" in str(caught.value)
    assert ".tmp" not in str(caught.value)


@needs_permissions
def test_preflight_refuses_an_existing_file_in_an_unwritable_directory(read_only_directory):
    read_only_directory.chmod(0o700)
    target = read_only_directory / "d.json"
    target.write_text("{}")
    read_only_directory.chmod(0o500)
    with pytest.raises(PermissionError, match="nothing was written"):
        preflight_dump_target(target)


def test_preflight_accepts_an_existing_own_file_and_a_missing_target(tmp_path):
    existing = tmp_path / "dump.json"
    existing.write_text("{}")
    preflight_dump_target(existing)
    preflight_dump_target(tmp_path / "missing.json")
    preflight_dump_target(tmp_path / "a" / "b" / "missing.json")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dump.json"]  # nothing created
