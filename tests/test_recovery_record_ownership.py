"""Recovery intent and failure counters are trusted only from our own regular file."""

from __future__ import annotations

import json
import os

import pytest
from test_autorestore import FakeRouter, Fetcher, make_dump, reset_pages

from bgwcli import recovery_state
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.recovery_state import RecoveryCheckpoint


def store(tmp_path):
    return RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")


def test_a_symlinked_record_is_a_fault_not_an_intent(tmp_path):
    checkpoint = store(tmp_path)
    checkpoint.begin()
    genuine = checkpoint.path.with_name("genuine.json")
    checkpoint.path.rename(genuine)
    checkpoint.path.symlink_to(genuine)
    with pytest.raises(OSError):
        checkpoint.is_active()
    with pytest.raises(OSError):
        checkpoint.failure_count()
    with pytest.raises(OSError):
        checkpoint.begin()
    assert checkpoint.path.is_symlink()


def test_a_foreign_owned_record_is_a_fault_not_an_intent(tmp_path, monkeypatch):
    checkpoint = store(tmp_path)
    checkpoint.begin()
    uid = os.geteuid()
    monkeypatch.setattr(recovery_state.os, "geteuid", lambda: uid + 1)
    with pytest.raises(PermissionError, match="owned by another user"):
        checkpoint.is_active()
    with pytest.raises(PermissionError):
        checkpoint.record_failure("x")


def test_an_unsafe_record_never_authorizes_a_write_for_ordinary_drift(tmp_path):
    checkpoint = store(tmp_path)
    checkpoint.begin()
    genuine = checkpoint.path.with_name("genuine.json")
    checkpoint.path.rename(genuine)
    checkpoint.path.symlink_to(genuine)
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages()), checkpoint=checkpoint, log=lambda _: None,
    )
    assert result.status == "error" and "recovery checkpoint read failed" in result.reason
    assert router.posts == []


def test_our_own_regular_record_still_works(tmp_path):
    checkpoint = store(tmp_path)
    checkpoint.begin()
    assert checkpoint.is_active() is True
    assert json.loads(checkpoint.path.read_text())["origin"]
    assert checkpoint.record_failure("a") == 1
