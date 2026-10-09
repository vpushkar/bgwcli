"""Stale leftovers of crashed writers are reaped; the legacy stale-lock setting is validated like the rest."""

from __future__ import annotations

import os
import time
import uuid

import pytest

from bgwcli import cli, session
from bgwcli.errors import UsageError

ORIGIN = "http://router.local"
OPTIONS = session.SessionCoordinatorOptions(120000, 300000, 1000, False)


def _aged(path, seconds):
    old = time.time() - seconds
    os.utime(path, (old, old))


def _prepared(tmp_env):
    paths = session.session_paths(ORIGIN)
    session._ensure_private_dir(paths.cache.parent)
    return paths


def test_old_own_lock_and_temp_leftovers_are_reaped_young_and_live_files_stay(tmp_env):
    paths = _prepared(tmp_env)
    directory = paths.cache.parent
    stale = [
        directory / ".bgw-lock-crashed1",
        directory / f"{paths.cache.name}.123.{uuid.uuid4()}.tmp",
        directory / f"{paths.cooldown.name}.456.{uuid.uuid4()}.tmp",
    ]
    for path in stale:
        path.write_text("x")
        _aged(path, 3600)
    young = directory / ".bgw-lock-young"
    young.write_text("x")
    keep = [paths.cache, paths.cooldown, paths.lock, directory / "notes.txt"]
    for path in keep:
        path.write_text("{}")
        _aged(path, 7200)
    session._ensure_private_dir(directory)
    assert not any(path.exists() for path in stale)
    assert young.exists() and all(path.exists() for path in keep)


def test_an_integer_too_large_for_a_float_makes_a_record_unsound_without_raising():
    assert session._cache_record_sound({"expiresAt": 10**400}) is False
    assert session._cache_record_sound({"cachedAt": -(10**400), "expiresAt": 1}) is False
    assert session._cooldown_record_sound({"until": 10**400}) is False
    assert session._int_field({"until": 10**400}, "until") == 0
    assert session._cache_record_sound({"cachedAt": 1, "expiresAt": 2}) is True


@pytest.mark.parametrize("name", [
    ".bgw-lock-personal-notes", ".bgw-lock-", ".bgw-lock-short", ".bgw-lock-toolongname", ".bgw-lock-UPPER123",
    ".bgw-lock-abc.def", ".bgw-lock-abcdefgh.bak", "x.bgw-lock-abcdefgh",
    "notes.tmp", "unrelated-notes.tmp", "abc.session.json.tmp", "abc.session.json.123.dead.tmp",
    "abc.other.json.123.00000000-0000-0000-0000-000000000000.tmp", ".recovery-abcd1234", "x.session.json",
])
def test_a_file_that_is_not_one_of_our_writer_temporaries_is_never_touched(tmp_env, name):
    paths = _prepared(tmp_env)
    foreign = paths.cache.parent / name
    foreign.write_text("keep")
    _aged(foreign, 7200)
    session._ensure_private_dir(paths.cache.parent)
    assert foreign.read_text() == "keep"


def test_stale_recovery_temporaries_are_reaped_and_nothing_else_in_that_directory(tmp_env):
    from bgwcli.recovery_state import RecoveryCheckpoint
    from bgwcli.snapshot import Snapshot, SnapshotMeta

    root = tmp_env / "state" / "bgw" / "recovery"
    store = RecoveryCheckpoint("router.local", Snapshot(SnapshotMeta("", "", "router.local")), None, root=root)
    store.begin()
    directory = store.path.parent
    stale, young = directory / ".recovery-crashed1", directory / ".recovery-young001"
    notes = directory / "notes.tmp"
    for path in (stale, young, notes):
        path.write_text("x")
    _aged(stale, 7200)
    _aged(notes, 7200)
    store.begin()
    assert not stale.exists() and young.exists() and notes.exists() and store.path.exists()


def test_a_symlink_or_directory_with_a_leftover_name_is_never_followed_or_removed(tmp_env, tmp_path):
    paths = _prepared(tmp_env)
    target = tmp_path / "target.txt"
    target.write_text("keep")
    link = paths.cache.parent / ".bgw-lock-link"
    link.symlink_to(target)
    directory = paths.cache.parent / "x.tmp"
    directory.mkdir()
    _aged(directory, 3600)
    session._ensure_private_dir(paths.cache.parent)
    assert target.read_text() == "keep" and link.is_symlink() and directory.is_dir()


def test_a_foreign_owned_leftover_is_preserved(tmp_env, monkeypatch):
    paths = _prepared(tmp_env)
    leftover = paths.cache.parent / ".bgw-lock-foreign1"
    leftover.write_text("x")
    _aged(leftover, 3600)
    real = session._owned_by_us
    monkeypatch.setattr(session, "_owned_by_us", lambda info: False)
    session._ensure_private_dir(paths.cache.parent)
    assert leftover.exists()
    monkeypatch.setattr(session, "_owned_by_us", real)
    session._ensure_private_dir(paths.cache.parent)
    assert not leftover.exists()


@pytest.mark.parametrize("value", ["garbage", "NaN", "Infinity", "-1"])
def test_an_invalid_stale_lock_setting_is_a_usage_error(tmp_env, monkeypatch, capsys, value):
    monkeypatch.setenv("BGW_SESSION_LOCK_STALE_MS", value)
    with pytest.raises(UsageError, match="BGW_SESSION_LOCK_STALE_MS"):
        session._lock_stale_ms()
    assert cli.main(["session", "clear-cache", "--host", ORIGIN, "--json"]) == 1
    capsys.readouterr()


def test_a_valid_stale_lock_setting_is_used(tmp_env, monkeypatch):
    monkeypatch.setenv("BGW_SESSION_LOCK_STALE_MS", "1500.5")
    assert session._lock_stale_ms() == 1500.5
    monkeypatch.delenv("BGW_SESSION_LOCK_STALE_MS")
    assert session._lock_stale_ms() == session._DEFAULT_LOCK_STALE_MS
