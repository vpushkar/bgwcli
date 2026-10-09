"""Session cache privacy is established before callbacks or local session writes."""

import os
import signal
import stat
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from bgwcli import filesystem, session


def coordinate(calls=None):
    class Client:
        def session_identity(self):
            return "https://router.invalid"

        def has_authenticated_session(self):
            return False

    def callback():
        if calls is not None:
            calls.append("callback ran")
        return "callback ran"

    return session.with_router_session(Client(), session.SessionCoordinatorOptions(1000, 1000, 1000, False), callback)


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "foreign-owner", "file"])
def test_unsafe_cache_leaf_stops_callback_and_preserves_entries(tmp_path, monkeypatch, kind):
    leaf = tmp_path / "cache"
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve target")
    if kind == "symlink":
        leaf.symlink_to(target, target_is_directory=True)
    elif kind == "dangling-symlink":
        leaf.symlink_to(tmp_path / "missing", target_is_directory=True)
    elif kind == "file":
        leaf.write_text("preserve file")
    else:
        leaf.mkdir()
        leaf.chmod(0o755)
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    before = leaf.lstat()
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    monkeypatch.setenv("BGW_ACCESS_CODE", "SECRET-DO-NOT-REPORT")
    calls = []
    with pytest.raises(session.SessionLockError) as failure:
        coordinate(calls)
    assert calls == []
    assert isinstance(failure.value.__cause__, OSError)
    assert str(leaf) in str(failure.value)
    assert "SECRET-DO-NOT-REPORT" not in str(failure.value)
    assert leaf.lstat().st_mode == before.st_mode
    assert leaf.lstat().st_ino == before.st_ino
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve target"
    if kind == "file":
        assert leaf.read_text() == "preserve file"
    elif kind == "foreign-owner":
        assert list(leaf.iterdir()) == []
    elif kind == "dangling-symlink":
        assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("mode", [0, 0o755, 0o700])
def test_owned_cache_leaf_becomes_private_before_callback(tmp_path, monkeypatch, mode):
    leaf = tmp_path / "cache"
    leaf.mkdir()
    leaf.chmod(mode)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    try:
        assert coordinate() == "callback ran"
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    finally:
        leaf.chmod(0o700)


def test_missing_cache_parents_under_restrictive_umask(tmp_path, monkeypatch):
    tmp_path.chmod(0o751)
    first = tmp_path / "first"
    second = first / "second"
    leaf = second / "cache"
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    previous = os.umask(0o777)
    try:
        assert coordinate() == "callback ran"
        assert [stat.S_IMODE(p.stat().st_mode) for p in (first, second, leaf)] == [0o700] * 3
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o751
    finally:
        os.umask(previous)
        for path in (first, second, leaf):
            if path.exists():
                path.chmod(0o700)


def test_existing_inaccessible_cache_parent_is_preserved(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory access permissions")
    parent = tmp_path / "established"
    parent.mkdir()
    parent.chmod(0)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(parent / "cache"))
    try:
        with pytest.raises(session.SessionLockError):
            coordinate()
        assert stat.S_IMODE(parent.stat().st_mode) == 0
    finally:
        parent.chmod(0o700)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("explicit", [False, True])
def test_cache_parent_alias_preserves_established_base(tmp_path, monkeypatch, explicit):
    base = tmp_path / "base"
    base.mkdir()
    base.chmod(0o751)
    alias = tmp_path / "alias"
    alias.symlink_to(base, target_is_directory=True)
    monkeypatch.delenv("BGW_SESSION_CACHE_DIR", raising=False)
    if explicit:
        monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(alias / "bgw"))
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(alias))
    assert coordinate() == "callback ran"
    assert stat.S_IMODE(base.stat().st_mode) == 0o751
    assert stat.S_IMODE((base / "bgw").stat().st_mode) == 0o700


def test_explicit_current_directory_is_private_cache_leaf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tmp_path.chmod(0o755)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", ".")
    assert coordinate() == "callback ran"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


@pytest.mark.parametrize("spelling", ["/", "/..", "alias"])
def test_filesystem_root_is_refused_without_mutation(tmp_path, monkeypatch, spelling):
    alias = tmp_path / "root-alias"
    alias.symlink_to("/", target_is_directory=True)
    chosen = str(alias) if spelling == "alias" else spelling
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", chosen)

    def forbid_mutation(*args, **kwargs):
        pytest.fail("filesystem root refusal must precede any mutation")

    monkeypatch.setattr(Path, "mkdir", forbid_mutation)
    monkeypatch.setattr(Path, "chmod", forbid_mutation)
    monkeypatch.setattr(os, "chmod", forbid_mutation)
    with pytest.raises(session.SessionLockError, match="filesystem root"):
        coordinate()


def test_cache_leaf_swap_during_permission_change_preserves_target(tmp_path, monkeypatch):
    leaf = tmp_path / "cache"
    leaf.mkdir()
    leaf.chmod(0o755)
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    original_chmod = Path.chmod

    def replace_leaf(path, mode, *, follow_symlinks=True):
        if path == leaf.parent.resolve() / leaf.name:
            leaf.rename(tmp_path / "displaced")
            leaf.symlink_to(target, target_is_directory=True)
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", replace_leaf)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    with pytest.raises(session.SessionLockError):
        coordinate()
    assert leaf.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []


def test_cache_uses_descriptor_fallback_when_nofollow_chmod_unavailable(tmp_path, monkeypatch):
    leaf = tmp_path / "cache"
    leaf.mkdir()
    leaf.chmod(0o500)
    original_chmod = Path.chmod

    def unsupported(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("no-follow chmod unsupported")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", unsupported)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(leaf))
    assert coordinate() == "callback ran"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("kind", ["dangling", "nested-dangling", "file", "self-loop", "cycle"])
def test_invalid_cache_parent_alias_is_preserved_before_publication(tmp_path, monkeypatch, explicit, kind):
    alias = tmp_path / "alias"
    target = tmp_path / "target"
    if kind == "cycle":
        target.symlink_to(alias, target_is_directory=True)
    elif kind == "nested-dangling":
        target.symlink_to(tmp_path / "missing", target_is_directory=True)
    elif kind == "file":
        target.write_text("preserve file")
    alias.symlink_to(alias if kind == "self-loop" else target, target_is_directory=True)
    before = {p.name: p.lstat() for p in tmp_path.iterdir()}
    monkeypatch.delenv("BGW_SESSION_CACHE_DIR", raising=False)
    if explicit:
        monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(alias / "ordinary" / "cache"))
    else:
        monkeypatch.setenv("XDG_CACHE_HOME", str(alias / "ordinary"))
    calls = []
    with pytest.raises(session.SessionLockError) as failure:
        coordinate(calls)
    assert isinstance(failure.value.__cause__, OSError)
    assert calls == []
    assert {p.name for p in tmp_path.iterdir()} == set(before)
    for name, info in before.items():
        after = (tmp_path / name).lstat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (info.st_ino, info.st_mode, info.st_mtime_ns)


_CACHE_SUBPROCESS = f"""
import os
from pathlib import Path
import signal
import sys
# pytest's pythonpath setting does not reach child interpreters.
sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
from bgwcli import session
leaf = Path(sys.argv[1])
crash = sys.argv[2]
if crash:
    mkdir = Path.mkdir
    def interrupted(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        if path.name == crash:
            os.kill(os.getpid(), signal.SIGKILL)
    Path.mkdir = interrupted
os.umask(0o777)
session._ensure_private_dir(leaf)
print('prepared')
"""


@pytest.mark.parametrize("crash_name", ["first", "second"])
def test_fresh_process_repairs_cache_parent_after_sigkill(tmp_path, crash_name):
    first = tmp_path / "first"
    second = first / "second"
    leaf = second / "cache"
    paths = (first, second, leaf)
    interrupted = first if crash_name == "first" else second
    try:
        crashed = subprocess.run(
            [sys.executable, "-c", _CACHE_SUBPROCESS, str(leaf), crash_name],
            capture_output=True, text=True, timeout=10,
        )
        assert crashed.returncode == -signal.SIGKILL, crashed.stderr
        assert stat.S_IMODE(interrupted.stat().st_mode) == 0
        pending = list(interrupted.parent.glob(".bgw-publication-*.pending"))
        assert len(pending) == 1
        assert stat.S_IMODE(pending[0].stat().st_mode) == 0o600
        retried = subprocess.run(
            [sys.executable, "-c", _CACHE_SUBPROCESS, str(leaf), ""],
            capture_output=True, text=True, timeout=10,
        )
        assert retried.returncode == 0, retried.stderr
        assert retried.stdout.strip() == "prepared"
        assert [stat.S_IMODE(p.stat().st_mode) for p in paths] == [0o700] * 3
        assert not list(tmp_path.rglob(".bgw-publication-*.pending"))
    finally:
        for path in paths:
            with suppress(FileNotFoundError):
                path.chmod(0o700)


def test_fresh_process_preserves_unmarked_inaccessible_cache_parent(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory access permissions")
    parent = tmp_path / "existing"
    parent.mkdir()
    before = parent.stat()
    parent.chmod(0)
    try:
        retried = subprocess.run(
            [sys.executable, "-c", _CACHE_SUBPROCESS, str(parent / "cache"), ""],
            capture_output=True, text=True, timeout=10,
        )
        assert retried.returncode != 0 and "SessionLockError" in retried.stderr
        assert parent.stat().st_ino == before.st_ino
        assert stat.S_IMODE(parent.stat().st_mode) == 0
        assert list(tmp_path.iterdir()) == [parent]
    finally:
        parent.chmod(0o700)
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["malformed", "symlink", "shared", "foreign-owner"])
@pytest.mark.parametrize("mode", [0, 0o755])
def test_unsafe_parent_publication_marker_preserves_cache_parent(tmp_path, monkeypatch, kind, mode):
    parent = tmp_path / "pending"
    parent.mkdir()
    marker = filesystem._publication_marker(parent)
    target = tmp_path / "foreign-data"
    target.write_text("preserve")
    target.chmod(0o600)
    if kind == "symlink":
        marker.symlink_to(target)
    elif kind == "shared":
        marker.hardlink_to(target)
    else:
        marker.write_bytes(b"foreign record" if kind == "malformed" else b"")
        marker.chmod(0o600)
    before = marker.lstat()
    if kind == "foreign-owner":
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    parent.chmod(mode)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(parent / "cache"))
    try:
        with pytest.raises(session.SessionLockError):
            coordinate()
        assert stat.S_IMODE(parent.stat().st_mode) == mode
        after = marker.lstat()
        assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_ino, before.st_mode, before.st_mtime_ns)
        assert target.read_text() == "preserve"
    finally:
        parent.chmod(0o700)
    assert list(parent.iterdir()) == []


def test_cache_parent_alias_supports_missing_ordinary_suffix(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir(mode=0o751)
    alias = tmp_path / "alias"
    alias.symlink_to(base, target_is_directory=True)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(alias / "first" / "second" / "cache"))
    assert coordinate() == "callback ran"
    assert stat.S_IMODE(base.stat().st_mode) == 0o751
    assert stat.S_IMODE((base / "first" / "second" / "cache").stat().st_mode) == 0o700


def test_missing_parent_before_dotdot_is_created_before_traversal(tmp_path, monkeypatch):
    tmp_path.chmod(0o751)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("preserve established contents")
    before = tmp_path.stat()
    create_marker = filesystem._create_publication_marker
    published = []

    def record_publication(marker, directory):
        published.append(directory)
        assert directory == tmp_path / "required", "established ancestors must not acquire publication authority"
        return create_marker(marker, directory)

    monkeypatch.setattr(filesystem, "_create_publication_marker", record_publication)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(tmp_path / "required" / ".." / "cache"))
    assert coordinate() == "callback ran"
    assert published == [tmp_path / "required"]
    assert (tmp_path / "required").is_dir()
    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700
    assert (tmp_path.stat().st_ino, tmp_path.stat().st_mode) == (before.st_ino, before.st_mode)
    assert sentinel.read_text() == "preserve established contents"
    assert {path.name for path in tmp_path.iterdir()} == {"required", "cache", "sentinel"}


def test_parent_dotdot_normalized_to_root_never_publishes_existing_ancestors(tmp_path, monkeypatch):
    required = tmp_path / "required"
    directory = required.joinpath(*(".." for _ in required.parts[1:]))
    create_marker = filesystem._create_publication_marker
    mkdir = Path.mkdir
    published = []

    def confined_publication(marker, target):
        published.append(target)
        assert target == required, "no publication outside the missing fixture directory is permitted"
        return create_marker(marker, target)

    def confined_mkdir(target, *args, **kwargs):
        assert target == required, "no mkdir outside the missing fixture directory is permitted"
        return mkdir(target, *args, **kwargs)

    monkeypatch.setattr(filesystem, "_create_publication_marker", confined_publication)
    monkeypatch.setattr(Path, "mkdir", confined_mkdir)
    filesystem.ensure_directory(directory, allow_aliases=True)
    assert required.is_dir()
    assert published == [required]
    assert list(tmp_path.iterdir()) == [required]


@pytest.mark.parametrize("dangling", [False, True])
def test_parent_alias_is_rechecked_after_pending_ancestor_repair(tmp_path, monkeypatch, dangling):
    pending = tmp_path / "pending"
    pending.mkdir()
    target = tmp_path / "target"
    if not dangling:
        target.mkdir(mode=0o751)
    alias = pending / "alias"
    alias.symlink_to(target, target_is_directory=True)
    marker = filesystem._publication_marker(pending)
    marker.write_bytes(filesystem._marker_content(pending))
    marker.chmod(0o600)
    pending.chmod(0)
    monkeypatch.setenv("BGW_SESSION_CACHE_DIR", str(alias / "cache"))
    try:
        if dangling:
            with pytest.raises(session.SessionLockError):
                coordinate()
            assert not target.exists()
        else:
            assert coordinate() == "callback ran"
            assert stat.S_IMODE(target.stat().st_mode) == 0o751
        assert alias.is_symlink()
    finally:
        pending.chmod(0o700)
