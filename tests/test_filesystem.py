"""Permission repair contracts shared by cache and recovery directories."""

import errno
import os
import stat
from pathlib import Path

import pytest

from bgwcli import filesystem
from bgwcli.filesystem import restore_owner_access


def _identity(info):
    """Entry identity without st_atime, which reads may advance under relatime/strictatime."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns)


@pytest.mark.parametrize("private, mode", [(True, 0o700), (False, 0o751)])
def test_validated_directory_with_complete_permissions_needs_no_chmod(tmp_path, monkeypatch, private, mode):
    directory = tmp_path / "complete"
    directory.mkdir()
    directory.chmod(mode)

    def deny_chmod(*args, **kwargs):
        raise PermissionError("permission changes unavailable")

    monkeypatch.setattr(Path, "chmod", deny_chmod)
    restore_owner_access(directory, private=private)
    assert stat.S_IMODE(directory.stat().st_mode) == mode


@pytest.mark.parametrize("kind", ["foreign-owner", "symlink", "file"])
def test_complete_permissions_do_not_bypass_entry_validation(tmp_path, monkeypatch, kind):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    directory = tmp_path / "entry"
    if kind == "symlink":
        directory.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        directory.write_text("preserve")
        directory.chmod(0o700)
    else:
        directory.mkdir(mode=0o700)
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(PermissionError, match="preserved"):
        restore_owner_access(directory, private=True)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("spelling", ["absolute", "parent", "alias", "current", "relative-parent"])
def test_filesystem_root_guard_refuses_aliases_before_mutation(tmp_path, monkeypatch, spelling):
    alias = tmp_path / "root-alias"
    alias.symlink_to("/", target_is_directory=True)
    if spelling == "alias":
        directory = alias
    elif spelling in {"current", "relative-parent"}:
        monkeypatch.chdir("/")
        directory = Path("." if spelling == "current" else "..")
    else:
        directory = Path("/" if spelling == "absolute" else "/..")

    def forbid_mutation(*args, **kwargs):
        pytest.fail("root guard must be read-only")

    monkeypatch.setattr(Path, "mkdir", forbid_mutation)
    monkeypatch.setattr(Path, "chmod", forbid_mutation)
    monkeypatch.setattr(Path, "unlink", forbid_mutation)
    monkeypatch.setattr(os, "chmod", forbid_mutation)
    with pytest.raises(PermissionError, match="Test directory.*filesystem root.*preserved"):
        filesystem.reject_filesystem_root(directory, description="Test directory")


@pytest.mark.parametrize("kind", ["directory-alias", "self-loop", "parent-loop"])
def test_filesystem_root_guard_leaves_nonroot_aliases_for_caller_validation(tmp_path, monkeypatch, kind):
    alias = tmp_path / "alias"
    if kind == "directory-alias":
        alias.symlink_to(tmp_path, target_is_directory=True)
        directory = alias
    else:
        alias.symlink_to(alias, target_is_directory=True)
        directory = alias if kind == "self-loop" else alias / "child"

    def forbid_strict_resolution(*args, **kwargs):
        pytest.fail("strict resolution would change cyclic-path exceptions on older Python")

    monkeypatch.setattr(Path, "resolve", forbid_strict_resolution)
    filesystem.reject_filesystem_root(directory)


def test_existing_file_base_is_refused_without_publication_marker(tmp_path):
    directory = tmp_path / "base"
    directory.write_text("preserve existing file")
    before = directory.stat()

    with pytest.raises(NotADirectoryError, match="Recovery directory.*not a directory.*preserved"):
        filesystem.ensure_directory(directory, description="Recovery directory")

    assert list(tmp_path.iterdir()) == [directory]
    assert _identity(directory.stat()) == _identity(before)
    assert directory.read_text() == "preserve existing file"


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_symlink_base_is_diagnosed_as_symlink_regardless_of_target(tmp_path, target_kind):
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("preserve target file")
    else:
        target.mkdir(mode=0o700)
    directory = tmp_path / "state"
    directory.symlink_to(target, target_is_directory=target_kind == "directory")
    before = target.lstat()

    with pytest.raises(PermissionError, match="Recovery directory is a symlink; preserved"):
        filesystem.ensure_directory(directory, description="Recovery directory")

    assert directory.is_symlink()
    assert _identity(target.lstat()) == _identity(before)
    if target_kind == "file":
        assert target.read_text() == "preserve target file"
    else:
        assert list(target.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state", "target"]


def test_alias_resolution_failure_names_the_described_directory(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("preserve file")
    before = occupied.lstat()

    with pytest.raises(NotADirectoryError, match="Session cache parent.*not a directory.*preserved") as caught:
        filesystem.ensure_directory(occupied / "child", description="Session cache parent", allow_aliases=True)

    assert "Session cache parent" in str(caught.value)
    assert _identity(occupied.lstat()) == _identity(before)
    assert list(tmp_path.iterdir()) == [occupied]


@pytest.mark.parametrize("existing_marker", [False, True])
def test_file_racing_base_mkdir_preserves_file_and_reservation(tmp_path, monkeypatch, existing_marker):
    directory = tmp_path / "base"
    marker = filesystem._publication_marker(directory)
    if existing_marker:
        marker.write_bytes(filesystem._marker_content(directory))
        marker.chmod(0o600)
        marker_before = marker.stat()
    original_mkdir = Path.mkdir

    def race_file(path, *args, **kwargs):
        if path == directory:
            path.write_text("concurrent file")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_file)
    with pytest.raises(NotADirectoryError, match="Recovery directory.*not a directory.*preserved"):
        filesystem.ensure_directory(directory, description="Recovery directory")

    assert directory.read_text() == "concurrent file"
    assert marker.exists()
    if existing_marker:
        assert _identity(marker.stat()) == _identity(marker_before)

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    directory.unlink()
    filesystem.ensure_directory(directory, description="Recovery directory")
    assert directory.is_dir() and not marker.exists()


@pytest.mark.parametrize("existing_marker", [False, True])
def test_raced_file_disappearing_after_diagnosis_preserves_error_and_reservation(
    tmp_path, monkeypatch, existing_marker,
):
    directory = tmp_path / "base"
    marker = filesystem._publication_marker(directory)
    if existing_marker:
        marker.write_bytes(filesystem._marker_content(directory))
        marker.chmod(0o600)
        marker_before = marker.stat()
    original_mkdir = Path.mkdir
    original_lstat = Path.lstat
    observations = []

    def race_file(path, *args, **kwargs):
        if path == directory:
            path.write_text("concurrent file")
        return original_mkdir(path, *args, **kwargs)

    def remove_after_diagnosis(path):
        info = original_lstat(path)
        if path == directory and stat.S_ISREG(info.st_mode):
            observations.append(info)
            path.unlink()  # Diagnosed as a file; any later probe would find no entry.
        return info

    monkeypatch.setattr(Path, "mkdir", race_file)
    monkeypatch.setattr(Path, "lstat", remove_after_diagnosis)
    with pytest.raises(NotADirectoryError, match="Recovery directory.*not a directory.*preserved"):
        filesystem.ensure_directory(directory, description="Recovery directory")

    assert len(observations) == 1, "the single diagnosing probe must not be followed by a cleanup probe"
    assert not directory.exists()
    assert marker.exists()
    if existing_marker:
        assert _identity(marker.stat()) == _identity(marker_before)

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    monkeypatch.setattr(Path, "lstat", original_lstat)
    filesystem.ensure_directory(directory, description="Recovery directory")
    assert directory.is_dir() and not marker.exists()


def test_file_racing_base_mkdir_does_not_remove_replacement_marker(tmp_path, monkeypatch):
    directory = tmp_path / "base"
    marker = filesystem._publication_marker(directory)
    displaced = tmp_path / "original-marker"
    original_mkdir = Path.mkdir

    def race_file_and_marker(path, *args, **kwargs):
        if path == directory:
            path.write_text("concurrent file")
            marker.rename(displaced)
            marker.write_bytes(displaced.read_bytes())
            marker.chmod(0o600)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_file_and_marker)
    with pytest.raises(NotADirectoryError, match="not a directory.*preserved"):
        filesystem.ensure_directory(directory)

    assert marker.read_bytes() == displaced.read_bytes()
    assert marker.stat().st_ino != displaced.stat().st_ino
    assert displaced.read_bytes() == filesystem._marker_content(directory)
    assert directory.read_text() == "concurrent file"


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOTSUP, errno.EIO])
def test_directory_sync_failure_retains_publication_for_retry(tmp_path, monkeypatch, error_number):
    directory = tmp_path / "base"
    marker = filesystem._publication_marker(directory)
    original_fsync = os.fsync
    failure = OSError(error_number, "injected directory sync failure")

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise failure
        return original_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory_sync)
        with pytest.raises(OSError) as caught:
            filesystem.ensure_directory(directory)
    assert caught.value.errno == error_number
    if error_number in (errno.EINVAL, errno.ENOTSUP):
        assert str(tmp_path) in str(caught.value)
        assert "Directory fsync is required" in str(caught.value)
        assert "BGW_SESSION_CACHE_DIR" in str(caught.value)
        assert "recovery state directory" in str(caught.value)
        assert ".bgw-publication-*.pending" in str(caught.value)
        assert caught.value.__cause__ is failure
    else:
        assert caught.value is failure
    assert directory.is_dir() and marker.exists()

    filesystem.ensure_directory(directory)
    assert directory.is_dir() and not marker.exists()
