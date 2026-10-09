"""Recovery intent survives invocations without replaying stale writes."""

import json
import multiprocessing
import os
import stat
from dataclasses import replace

import pytest
from page_builders import apphosting_page, services_page
from test_autorestore import FakeRouter, Fetcher, full_pages, make_dump, reset_pages

from bgwcli import filesystem
from bgwcli.autorestore import AutorestoreOptions, result_output, run_autorestore
from bgwcli.errors import RouterAuthError, RouterConnectionError, SnapshotExtractionError
from bgwcli.recovery_state import RecoveryCheckpoint


def checkpoint(tmp_path, *, host="router.local", dump=None, pages=None):
    return RecoveryCheckpoint(host, dump or make_dump(), pages, root=tmp_path / "recovery")


def test_finish_refuses_symlink_with_recovery_context_and_preserves_checkpoint(tmp_path):
    store = checkpoint(tmp_path)
    store.begin()
    contents = store.path.read_bytes()
    displaced = tmp_path / "displaced"
    store.path.parent.rename(displaced)
    store.path.parent.symlink_to(displaced, target_is_directory=True)

    with pytest.raises(PermissionError, match="Recovery directory.*symlink.*preserved"):
        store.finish()

    assert store.path.parent.is_symlink()
    assert store.path.read_bytes() == contents


def run(fetcher, *, store=None, commit=True, dump=None, pages=None):
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), dump or make_dump(),
        AutorestoreOptions(commit=commit, max_passes=1, pages=pages),
        fetch_pages=fetcher, checkpoint=store, log=lambda _: None,
    )
    return result, router


def test_partial_recovery_resumes_in_another_invocation_from_fresh_state(tmp_path):
    partial = reset_pages()
    partial["services"] = services_page()
    first, router = run(Fetcher(reset_pages(), partial), store=checkpoint(tmp_path))
    assert first.status == "not-converged" and router.posts
    assert checkpoint(tmp_path).is_active()
    second, router = run(Fetcher(partial, full_pages()), store=checkpoint(tmp_path))
    assert second.status == "converged" and router.posts
    assert all(page != "services" for page, _ in router.posts)
    assert not checkpoint(tmp_path).is_active()


@pytest.mark.parametrize("change", ["host", "dump", "pages"])
def test_nonmatching_checkpoint_does_not_authorize_ordinary_drift(tmp_path, change):
    checkpoint(tmp_path).begin()
    kwargs = {
        "host": {"host": "another-router.local"},
        "dump": {"dump": replace(make_dump(), forms={"dosprotect": {"flood_protect": "off"}})},
        "pages": {"pages": ("services",)},
    }[change]
    partial = full_pages()
    partial["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    result, router = run(Fetcher(partial), store=checkpoint(tmp_path, **kwargs))
    assert result.status == "no-reset" and not router.posts
    assert checkpoint(tmp_path).is_active()


def test_identity_ignores_documentation_order_and_timestamp_but_hashes_requested_values(tmp_path):
    dump = make_dump()
    checkpoint(tmp_path, dump=dump, pages=("apphosting", "services")).begin()
    equivalent = replace(
        dump, meta=replace(dump.meta, ts="tomorrow", firmware="new"),
        services=list(reversed(dump.services)),
        forwards=[replace(f, device_label="new label") for f in dump.forwards],
        tables={"ipalloc": [{"notes": "not actionable"}]}, forms={"wconfig": {"ssid": "unselected"}},
    )
    assert checkpoint(tmp_path, host="https://ROUTER.local:443/", dump=equivalent,
                      pages=("services", "apphosting", "services")).is_active()
    changed = replace(equivalent, services=[replace(dump.services[0], ext_min_port=999)])
    assert not checkpoint(tmp_path, dump=changed, pages=("apphosting", "services")).is_active()


@pytest.mark.parametrize("active", [False, True])
def test_dry_run_neither_starts_nor_finishes_checkpoint(tmp_path, active):
    store = checkpoint(tmp_path)
    if active:
        store.begin()
    before = store.path.read_bytes() if active else None
    result, router = run(Fetcher(full_pages() if active else reset_pages()), store=store, commit=False)
    assert not router.posts
    assert store.is_active() == active
    assert (store.path.read_bytes() if store.path.exists() else None) == before


def test_corrupt_checkpoint_does_not_authorize_drift(tmp_path):
    store = checkpoint(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"version": 1, BROKEN')
    partial = reset_pages()
    partial["services"] = services_page()
    result, router = run(Fetcher(partial), store=store)
    assert result.status == "no-reset" and not router.posts


def test_failed_checkpoint_publication_prevents_router_writes(tmp_path):
    store = checkpoint(tmp_path)
    store.path.parent.write_text("not a directory")
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and result.exit_code == 2
    assert "checkpoint" in result.reason and not router.posts


def test_checkpoint_contains_only_private_identity_metadata(tmp_path):
    dump = replace(make_dump(), forms={"wconfig": {"wpa_key": "test-private-secret"}})
    store = checkpoint(tmp_path, dump=dump)
    store.begin()
    record = json.loads(store.path.read_text())
    assert set(record) == {"version", "origin", "fingerprint", "pages"}
    assert "test-private-secret" not in store.path.read_text()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert list(store.path.parent.iterdir()) == [store.path]
    checkpoint(tmp_path, dump=make_dump()).finish()
    assert store.is_active()  # Another baseline must not remove this intent.


@pytest.mark.parametrize("error", [RouterAuthError("session rejected"), RouterConnectionError("offline"),
                                   SnapshotExtractionError("invalid live snapshot")])
def test_closing_verification_error_keeps_execution_and_marks_diff_unavailable(error):
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, pages=("services",)),
        fetch_pages=Fetcher(reset_pages(), error), log=lambda _: None,
    )
    assert router.posts and result.status == "error" and result.exit_code == 2
    assert result.final_diff is None and len(result.passes) == 1
    assert result.passes[0]["applied"] == len(router.posts)
    assert result.passes[0]["converged"] is None


def test_actual_closing_extraction_error_retains_checkpoint_and_execution(tmp_path):
    broken = full_pages()
    broken["apphosting"] = apphosting_page(rows=[("custom_ssh", "offline-host")], device_options=[])
    store = checkpoint(tmp_path)
    result, router = run(Fetcher(reset_pages(), broken), store=store)
    assert router.posts and result.status == "error" and result.final_diff is None
    assert result.passes and store.is_active()


def test_atomic_publication_failure_preserves_existing_intent_and_stops_writes(tmp_path, monkeypatch):
    from bgwcli import recovery_state

    store = checkpoint(tmp_path)
    store.begin()
    original = store.path.read_bytes()

    def fail_publish(*_args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(recovery_state.os, "replace", fail_publish)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and not router.posts
    assert store.path.read_bytes() == original
    assert list(store.path.parent.iterdir()) == [store.path]


def test_failed_write_stops_this_invocation_but_next_replans_without_replaying_it(tmp_path):
    from bgwcli.types import HttpResponse

    class RejectedRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            self.posts.append((page, dict(fields)))
            return HttpResponse(500, "Error", {}, "save rejected", "https://router.local")

    store = checkpoint(tmp_path, pages=("services",))
    router = RejectedRouter()
    first = run_autorestore(
        lambda: (router, False), make_dump(),
        AutorestoreOptions(commit=True, max_passes=3, pages=("services",)),
        fetch_pages=Fetcher(reset_pages()), checkpoint=store, log=lambda _: None,
        sleep=lambda _: pytest.fail("failed writes must stop the current invocation"),
    )
    # An HTTP error answer to the write is no answer about it: exit 2, and the intent stays active.
    assert first.status == "error" and first.write_unanswered and len(router.posts) == 1 and len(first.passes) == 1
    assert store.is_active()
    # A later timer tick sees the first service present; never replay its rejected/uncertain POST.
    partial = reset_pages()
    partial["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    second, router = run(Fetcher(partial, full_pages()),
                         store=checkpoint(tmp_path, pages=("services",)), pages=("services",))
    assert second.status == "converged" and len(router.posts) == 1
    assert router.posts[0][1]["Service"] == "Mosh"
    assert not store.is_active()


def test_checkpoint_synchronizes_directory_after_publication_and_removal(tmp_path, monkeypatch):
    from bgwcli import recovery_state

    store = checkpoint(tmp_path)
    store.path.parent.mkdir()
    original_fsync = recovery_state.os.fsync
    states_at_sync = []

    def observe_sync(descriptor):
        info = recovery_state.os.fstat(descriptor)
        parent_info = store.path.parent.stat()
        if (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino):
            states_at_sync.append(store.path.exists())
        return original_fsync(descriptor)

    monkeypatch.setattr(recovery_state.os, "fsync", observe_sync)
    store.begin()
    assert states_at_sync == [True]
    store.finish()
    assert states_at_sync == [True, False]


def test_checkpoint_synchronizes_new_parent_directory_entries(tmp_path, monkeypatch):
    from bgwcli import recovery_state

    root = tmp_path / "new-state" / "bgw" / "recovery"
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    original_fsync = recovery_state.os.fsync
    synchronized = set()

    def observe_sync(descriptor):
        info = recovery_state.os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synchronized.add((info.st_dev, info.st_ino))
        return original_fsync(descriptor)

    monkeypatch.setattr(recovery_state.os, "fsync", observe_sync)
    store.begin()
    # Each mkdir creates an entry in its parent; publishing creates the file entry in recovery.
    for directory in (tmp_path, root.parent.parent, root.parent, root):
        info = directory.stat()
        assert (info.st_dev, info.st_ino) in synchronized


@pytest.mark.parametrize("phase", ["parents", "publication", "cleanup"])
def test_checkpoint_directory_sync_failure_stops_writes_or_retains_verified_evidence(
    tmp_path, monkeypatch, phase,
):
    from bgwcli import recovery_state

    store = checkpoint(tmp_path)
    if phase != "parents":
        store.path.parent.mkdir()
    original_fsync = recovery_state.os.fsync
    observed = []

    def fail_selected_sync(descriptor):
        if stat.S_ISDIR(recovery_state.os.fstat(descriptor).st_mode):
            observed.append(descriptor)
            if phase == "parents":
                raise OSError("simulated directory sync failure")
            info = recovery_state.os.fstat(descriptor)
            parent_info = store.path.parent.stat()
            is_record_directory = (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino)
            if is_record_directory and store.path.exists() == (phase == "publication"):
                raise OSError("simulated directory sync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(recovery_state.os, "fsync", fail_selected_sync)
    result, router = run(Fetcher(reset_pages(), full_pages()), store=store)
    assert result.status == "error" and result.exit_code == 2
    assert "checkpoint" in result.reason and "directory sync failure" in result.reason
    if phase == "cleanup":
        assert router.posts and result.final_diff.identical
        assert result.passes[0]["applied"] == len(router.posts)
        assert result.passes[0]["converged"] is True
        output = result_output(result)
        assert output["exitCode"] == 2 and output["diff"]["identical"] is True
        assert output["passes"][0]["converged"] is True
    else:
        assert not router.posts and result.passes == []
    assert observed
    for descriptor in observed:
        with pytest.raises(OSError):
            recovery_state.os.fstat(descriptor)  # Directory descriptors are closed even on failure.


@pytest.mark.parametrize("failed_parent", ["anchor", "new-state"])
def test_retry_resynchronizes_failed_ancestor_before_router_writes(tmp_path, monkeypatch, failed_parent):

    root = tmp_path / "new-state" / "bgw" / "recovery"
    failing_directory = tmp_path if failed_parent == "anchor" else root.parent.parent
    original_sync = filesystem.sync_directory
    attempts = []
    reject_sync = True

    def synchronize(directory):
        if directory == failing_directory:
            attempts.append(directory)
            if reject_sync:
                raise OSError("injected ancestor sync failure")
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", synchronize)
    for _ in range(2):
        # New store objects model separate invocations with no in-memory retry history.
        store = RecoveryCheckpoint("router.local", make_dump(), root=root)
        result, router = run(Fetcher(reset_pages(), full_pages()), store=store)
        assert result.status == "error" and result.exit_code == 2
        assert not router.posts and not store.path.exists()
    assert len(attempts) == 2

    reject_sync = False

    class CheckedRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            assert len(attempts) >= 3, "retry must sync the originally failed ancestor before writes"
            return super().post_cgi_page(page, fields)

    router = CheckedRouter()
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages(), full_pages()), checkpoint=store, log=lambda _: None,
    )
    assert result.status == "converged" and router.posts
    assert not store.is_active()


def test_checkpoint_setup_preserves_existing_ancestor_permissions(tmp_path):
    ancestor = tmp_path / "existing-state"
    ancestor.mkdir(mode=0o751)
    before = stat.S_IMODE(ancestor.stat().st_mode)
    store = RecoveryCheckpoint("router.local", make_dump(), root=ancestor / "bgw" / "recovery")
    store.begin()
    assert stat.S_IMODE(ancestor.stat().st_mode) == before
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("active", [False, True])
def test_readonly_preflight_failure_does_not_create_recovery_authority(tmp_path, active):
    class UnreadableRouter(FakeRouter):
        def get_cgi_page(self, page, **kwargs):
            raise RouterConnectionError("ownership unavailable")

    store = checkpoint(tmp_path)
    if active:
        store.begin()
    router = UnreadableRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True),
        fetch_pages=Fetcher(reset_pages()), checkpoint=store, log=lambda _: None,
    )
    assert result.status == "router-unreachable" and not router.posts
    assert store.is_active() == active
    partial = reset_pages()
    partial["services"] = services_page()
    if not active:
        later, router = run(Fetcher(partial), store=checkpoint(tmp_path))
        assert later.status == "no-reset" and not router.posts


@pytest.mark.parametrize("fixed, clear", [(True, True), (False, False)])
def test_refused_ownership_does_not_start_checkpoint(tmp_path, monkeypatch, fixed, clear):
    from bgwcli import autorestore
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight

    conflict = AllocationConflict("192.168.1.64", "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "holder", "off", fixed)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_args: AllocationPreflight(
        [conflict], {"Clear": "Clear"} if clear else None,
    ))
    store = checkpoint(tmp_path)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and not router.posts
    assert not store.is_active()


@pytest.mark.parametrize("kind", ["blocked", "noop"])
def test_nonexecutable_plan_does_not_start_checkpoint(tmp_path, monkeypatch, kind):
    from bgwcli import autorestore
    from bgwcli.restore import RestoreStep

    steps = [] if kind == "noop" else [RestoreStep(1, "service-add", "services", "blocked", blocked="unavailable")]
    monkeypatch.setattr(autorestore, "build_restore_plan", lambda *_args: steps)
    store = checkpoint(tmp_path)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert not router.posts and not store.is_active()
    assert result.status == "not-converged"


def test_checkpoint_ignores_unrelated_existing_ancestors(tmp_path, monkeypatch):

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    store = RecoveryCheckpoint("router.local", make_dump())
    original_sync = filesystem.sync_directory
    synced = []

    def sync(directory):
        synced.append(directory)
        assert directory == state or state in directory.parents, "unrelated ancestor must not be fsynced"
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    store.begin()
    assert set(synced) == {state, state / "bgw", state / "bgw" / "recovery"}
    assert store.is_active()


def test_proven_clear_nonce_failure_removes_only_new_intent(tmp_path, monkeypatch):
    from test_allocation_preflight import HOLDER, IP, devices_html, ipalloc_html, response

    from bgwcli.client import BGW320Client

    for active in (False, True):
        store = checkpoint(tmp_path)
        if active:
            store.begin()
        client = BGW320Client("synthetic.invalid", transport=lambda _request: pytest.fail("no transport expected"))
        reads = 0

        def get(page, **_kwargs):
            nonlocal reads
            reads += 1
            if reads > 2:
                raise RouterConnectionError("nonce unavailable")
            return response(devices_html([(IP, HOLDER, "old", "off")]) if page == "devices" else ipalloc_html())

        monkeypatch.setattr(client, "get_cgi_page", get)
        result = run_autorestore(
            lambda client=client: (client, False), make_dump(), AutorestoreOptions(commit=True),
            fetch_pages=Fetcher(reset_pages()), checkpoint=store, log=lambda _: None,
        )
        assert result.status == "error"
        assert result.allocation_preflight["clearAttempted"] is False
        assert store.is_active() == active


def test_checkpoint_exists_before_first_configuration_write(tmp_path):
    store = checkpoint(tmp_path)

    class CheckedRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            assert store.is_active()
            return super().post_cgi_page(page, fields)

    router = CheckedRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages(), full_pages()), checkpoint=store, log=lambda _: None,
    )
    assert result.status == "converged" and router.posts


def test_failed_new_intent_publication_does_not_authorize_later_drift(tmp_path, monkeypatch):

    store = checkpoint(tmp_path)
    original_sync = filesystem.sync_directory
    failed = False

    def fail_publication_once(directory):
        nonlocal failed
        if directory == store.path.parent and store.path.exists() and not failed:
            failed = True
            raise OSError("publication sync failed")
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", fail_publication_once)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and not router.posts
    assert not store.is_active()


@pytest.mark.parametrize("active", [False, True])
def test_deferred_read_failure_removes_only_new_intent(tmp_path, monkeypatch, active):
    from bgwcli import autorestore
    from bgwcli.restore import RestoreDeferredForward, RestoreStep

    store = checkpoint(tmp_path, pages=("apphosting",))
    if active:
        store.begin()
    step = RestoreStep(1, "forward-add", "apphosting", "resolve forwarding", deferred=RestoreDeferredForward(
        make_dump().forwards[0], "service added earlier",
    ))
    monkeypatch.setattr(autorestore, "build_restore_plan", lambda *_args: [step])

    class UnreadableRouter(FakeRouter):
        def get_cgi_page(self, page, **_kwargs):
            raise RouterConnectionError("forwarding page unavailable")

    router = UnreadableRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1, pages=("apphosting",)),
        fetch_pages=Fetcher(reset_pages()), checkpoint=store, log=lambda _: None,
    )
    assert result.status == "router-unreachable" and not router.posts
    assert result.passes[0]["steps"][0]["writeAttempted"] is False
    assert store.is_active() == active


@pytest.mark.parametrize("relative", [False, True])
def test_directory_boundary_is_validated_before_creating_anything(tmp_path, monkeypatch, relative):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    directory = Path("new/child") if relative else tmp_path / "new" / "child"
    boundary = Path("other") if relative else tmp_path / "other"
    with pytest.raises(ValueError, match="ancestor"):
        filesystem.ensure_directory(directory, boundary=boundary)
    assert list(tmp_path.iterdir()) == []


def test_concurrent_child_does_not_mask_sync_failure_or_allow_unsynced_retry(tmp_path, monkeypatch):

    root = tmp_path / "new-state" / "bgw" / "recovery"
    original_sync = filesystem.sync_directory
    reject_sync = True
    attempts = []

    def sync(directory):
        if directory == tmp_path:
            attempts.append(directory)
            (tmp_path / "new-state" / "concurrent-child").touch()
            if reject_sync:
                raise OSError("primary publication sync failure")
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    for _ in range(2):
        store = RecoveryCheckpoint("router.local", make_dump(), root=root)
        result, router = run(Fetcher(reset_pages()), store=store)
        assert result.status == "error" and not router.posts
        assert "primary publication sync failure" in result.reason
        assert not store.path.exists()
    assert len(attempts) == 2
    reject_sync = False
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    result, router = run(Fetcher(reset_pages(), full_pages()), store=store)
    assert result.status == "converged" and router.posts
    assert len(attempts) == 3
    assert (tmp_path / "new-state" / "concurrent-child").exists()


def test_begin_failure_remains_primary_when_unused_intent_cleanup_also_fails(tmp_path, monkeypatch):

    store = checkpoint(tmp_path)
    original_sync = filesystem.sync_directory

    def sync(directory):
        if directory == store.path.parent:
            raise OSError("primary begin durability failure" if store.path.exists() else "secondary cleanup failure")
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and result.exit_code == 2 and not router.posts
    assert result.reason.startswith("recovery checkpoint begin failed: primary begin durability failure")
    assert "unused intent cleanup failed: secondary cleanup failure" in result.reason
    assert not store.is_active()


def test_failed_pending_marker_creation_cannot_leave_an_unpublished_base(tmp_path, monkeypatch):
    from pathlib import Path

    from bgwcli import recovery_state

    root = tmp_path / "new-state" / "bgw" / "recovery"
    original_open = recovery_state.os.open

    def open_marker(path, *args, **kwargs):
        if Path(path).name.startswith(".bgw-publication-") and Path(path).suffix == ".pending":
            raise OSError("pending publication cannot be recorded")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(recovery_state.os, "open", open_marker)
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and not router.posts
    assert "pending publication cannot be recorded" in result.reason
    assert list(tmp_path.iterdir()) == []


def test_new_state_base_accepts_filesystem_maximum_directory_name(tmp_path):
    root = tmp_path / ("s" * 255) / "bgw" / "recovery"
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    store.begin()
    assert store.is_active()
    assert list(tmp_path.iterdir()) == [root.parent.parent]


def test_concurrent_publication_completion_does_not_fail_marker_cleanup(tmp_path, monkeypatch):

    directory = tmp_path / "new-state"
    original_sync = filesystem.sync_directory
    concurrent = False

    def sync(parent):
        nonlocal concurrent
        if parent == tmp_path and not concurrent:
            concurrent = True
            assert directory.is_dir(), "publication marker must not be consumed before mkdir"
            filesystem.ensure_directory(directory)
        original_sync(parent)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    filesystem.ensure_directory(directory)
    assert concurrent and directory.is_dir()
    assert list(tmp_path.iterdir()) == [directory]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("kind", ["symlink", "foreign-content", "unsafe-mode", "hardlink"])
def test_preplanted_publication_marker_is_refused_without_mutation(tmp_path, monkeypatch, existing, kind):

    directory = tmp_path / "state"
    if existing:
        directory.mkdir()
    marker = filesystem._publication_marker(directory)
    target = tmp_path / "unrelated"
    target.write_text("foreign data")
    target.chmod(0o600)
    if kind == "symlink":
        marker.symlink_to(target)
    elif kind == "hardlink":
        marker.hardlink_to(target)
    else:
        marker.write_text("foreign data" if kind == "foreign-content" else "")
        marker.chmod(0o600 if kind == "foreign-content" else 0o666)
    before = target.stat()
    marker_before = marker.lstat()
    original_sync = filesystem.sync_directory

    def sync(parent):
        assert parent != tmp_path, "foreign marker must not authorize unrelated parent sync"
        original_sync(parent)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    with pytest.raises((OSError, ValueError)) as caught:
        filesystem.ensure_directory(directory)
    assert str(marker) in str(caught.value)
    assert directory.exists() == existing
    after = marker.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        marker_before.st_ino, marker_before.st_mode, marker_before.st_mtime_ns,
    )
    assert target.stat() == before
    assert target.read_text() == "foreign data"


def test_existing_base_publication_probes_are_bounded_independent_of_ancestor_depth(tmp_path, monkeypatch):
    from pathlib import Path


    directory = tmp_path.joinpath(*("nested" for _ in range(25)))
    directory.mkdir(parents=True)
    original_stat = Path.stat
    probed = []

    def stat(path, *args, **kwargs):
        probed.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    filesystem.ensure_directory(directory)
    assert len(probed) <= 4
    assert all(path in (directory, filesystem._publication_marker(directory)) for path in probed)


def test_owned_private_empty_legacy_publication_marker_is_consumed(tmp_path):

    directory = tmp_path / "state"
    directory.mkdir()
    marker = filesystem._publication_marker(directory)
    marker.touch(mode=0o600)
    filesystem.ensure_directory(directory)
    assert not marker.exists()


def test_foreign_owned_empty_marker_is_not_legacy_compatibility(tmp_path, monkeypatch):
    from bgwcli import recovery_state

    directory = tmp_path / "state"
    directory.mkdir()
    marker = filesystem._publication_marker(directory)
    marker.touch(mode=0o600)
    actual_uid = recovery_state.os.geteuid()
    monkeypatch.setattr(recovery_state.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(PermissionError, match=str(marker)):
        filesystem.ensure_directory(directory)
    assert marker.exists() and marker.read_bytes() == b""


def test_publication_cleanup_preserves_replacement_marker(tmp_path, monkeypatch):

    directory = tmp_path / "state"
    marker = filesystem._publication_marker(directory)
    target = tmp_path / "unrelated"
    target.write_text("preserved")
    original_sync = filesystem.sync_directory

    def sync(parent):
        original_sync(parent)
        if parent == tmp_path:
            marker.unlink()
            marker.symlink_to(target)

    monkeypatch.setattr(filesystem, "sync_directory", sync)
    with pytest.raises(PermissionError, match=str(marker)):
        filesystem.ensure_directory(directory)
    assert marker.is_symlink()
    assert target.read_text() == "preserved"


def test_existing_filesystem_root_needs_no_publication_marker():
    from pathlib import Path


    filesystem.ensure_directory(Path("/"))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("alias_first", [False, True])
def test_failed_publication_retries_through_physical_path_alias(tmp_path, monkeypatch, legacy, alias_first):

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    first_parent, retry_parent = (alias, physical) if alias_first else (physical, alias)
    first_root = first_parent / "new-state" / "bgw" / "recovery"
    original_sync = filesystem.sync_directory

    def fail_sync(directory):
        if directory.resolve() == physical:
            raise OSError("injected alias publication failure")
        original_sync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(filesystem, "sync_directory", fail_sync)
        with pytest.raises(OSError, match="injected alias"):
            RecoveryCheckpoint("router.local", make_dump(), root=first_root).begin()
    pending = list(physical.glob(".bgw-publication-*.pending"))
    assert len(pending) == 1
    if legacy:
        pending[0].write_text(json.dumps({"version": 1, "directory": str(first_parent / "new-state")}))
    retry = RecoveryCheckpoint("router.local", make_dump(), root=retry_parent / "new-state" / "bgw" / "recovery")
    retry.begin()
    assert retry.is_active()
    assert not pending[0].exists()
    retry.finish()


@pytest.mark.parametrize("mask", [0o200, 0o400, 0o600])
def test_publication_marker_permissions_survive_restrictive_umask(tmp_path, mask):

    directory = tmp_path / "state"
    marker = filesystem._publication_marker(directory)
    previous = os.umask(mask)
    try:
        filesystem.ensure_directory(directory)
        assert directory.is_dir()
        assert not marker.exists()
    finally:
        os.umask(previous)
        if directory.exists():
            directory.chmod(0o700)
        if marker.exists():
            marker.chmod(0o600)


@pytest.mark.parametrize("record", [
    {"version": 1, "directory": "relative/state"},
    {"version": 1, "directory": "/unrelated/state"},
    {"version": 2, "directory": "CURRENT"},
    {"version": 1, "directory": "CURRENT", "foreign": True},
])
def test_foreign_directory_records_are_preserved(tmp_path, record):

    directory = tmp_path / "state"
    directory.mkdir()
    marker = filesystem._publication_marker(directory)
    record = dict(record)
    if record["directory"] == "CURRENT":
        record["directory"] = str(directory)
    marker.write_text(json.dumps(record))
    marker.chmod(0o600)
    before = marker.read_bytes()
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.ensure_directory(directory)
    assert marker.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing-parent", "foreign-symlink"])
def test_nonphysical_directory_alias_cannot_authorize_marker_cleanup(tmp_path, kind):

    directory = tmp_path / "state"
    directory.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    if kind == "missing-parent":
        recorded = tmp_path / "missing" / ".." / "state"
    else:
        recorded = tmp_path / "alias"
        recorded.symlink_to(foreign, target_is_directory=True)
    marker = filesystem._publication_marker(directory)
    marker.write_text(json.dumps({"version": 1, "directory": str(recorded)}))
    marker.chmod(0o600)
    before = marker.read_bytes()
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.ensure_directory(directory)
    assert marker.read_bytes() == before
    assert list(foreign.iterdir()) == []


def test_legacy_alias_marker_retries_before_directory_has_been_created(tmp_path):

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    directory = physical / "state"
    marker = filesystem._publication_marker(directory)
    marker.write_text(json.dumps({"version": 1, "directory": str(alias / "state")}))
    marker.chmod(0o600)
    RecoveryCheckpoint("router.local", make_dump(), root=directory / "bgw" / "recovery").begin()
    assert directory.is_dir()
    assert not marker.exists()


def test_checkpoint_file_is_private_and_readable_under_umask_0777(tmp_path):
    store = checkpoint(tmp_path)
    store.path.parent.mkdir(mode=0o700)
    previous = os.umask(0o777)
    try:
        store.begin()
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert store.is_active()
        store.finish()
        assert not store.path.exists()
    finally:
        os.umask(previous)


@pytest.mark.parametrize("mask", [0o200, 0o400, 0o600, 0o777])
def test_new_checkpoint_directories_remain_usable_under_restrictive_umask(tmp_path, mask):
    state = tmp_path / "new-state"
    root = state / "bgw" / "recovery"
    before = stat.S_IMODE(tmp_path.stat().st_mode)
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    previous = os.umask(mask)
    try:
        store.begin()
        assert store.is_active()
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(state.stat().st_mode) == 0o700 | (0o777 & ~mask)
        assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700 | (0o777 & ~mask)
        assert stat.S_IMODE(tmp_path.stat().st_mode) == before
        store.finish()
        assert not store.path.exists()
    finally:
        os.umask(previous)
        for directory in (state, root.parent, root):
            if directory.exists():
                directory.chmod(0o700)



def _crash_after_checkpoint_mkdir(state, crash_name):
    from pathlib import Path

    original_mkdir = Path.mkdir

    def interrupted_mkdir(directory, *args, **kwargs):
        original_mkdir(directory, *args, **kwargs)
        if directory.name == crash_name:
            os._exit(74)

    Path.mkdir = interrupted_mkdir
    os.environ["XDG_STATE_HOME"] = state
    os.umask(0o777)
    RecoveryCheckpoint("router.local", make_dump()).begin()


@pytest.mark.parametrize("crash_name", ["new-parent", "state", "bgw", "recovery"])
def test_retry_completes_directory_permissions_after_interrupted_mkdir(tmp_path, monkeypatch, crash_name):
    state = tmp_path / "new-parent" / "state"
    root = state / "bgw" / "recovery"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_crash_after_checkpoint_mkdir, args=(str(state), crash_name))
    child.start()
    child.join(5)
    previous = os.umask(0o777)
    try:
        assert child.exitcode == 74
        retry = RecoveryCheckpoint("router.local", make_dump())
        retry.begin()
        assert retry.is_active()
        for directory in (state.parent, state, root.parent, root):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        retry.finish()
        assert not retry.path.exists()
        assert not list(tmp_path.rglob(".bgw-publication-*.pending"))
    finally:
        os.umask(previous)
        if child.is_alive():
            child.terminate()
        child.join(5)
        for directory in (state.parent, state, root.parent, root):
            if directory.exists():
                directory.chmod(0o700)


@pytest.mark.parametrize("ancestor", [False, True])
def test_unmarked_inaccessible_base_permissions_are_never_repaired(tmp_path, monkeypatch, ancestor):
    existing = tmp_path / "existing"
    existing.mkdir()
    state = existing / "state" if ancestor else existing
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    existing.chmod(0)
    try:
        with pytest.raises(PermissionError):
            RecoveryCheckpoint("router.local", make_dump()).begin()
        assert stat.S_IMODE(existing.stat().st_mode) == 0
    finally:
        existing.chmod(0o700)
    assert list(existing.iterdir()) == []


@pytest.mark.parametrize("scope", ["pending-base", "owned-entry"])
def test_permission_repair_refuses_directory_symlinks(tmp_path, scope):

    target = tmp_path / "foreign"
    target.mkdir(mode=0o500)
    directory = tmp_path / "entry"
    directory.symlink_to(target, target_is_directory=True)
    marker = filesystem._publication_marker(directory)
    if scope == "pending-base":
        marker.write_text(json.dumps({"version": 1, "directory": str(directory)}))
        marker.chmod(0o600)
    before = stat.S_IMODE(target.stat().st_mode)
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.ensure_directory(directory, boundary=tmp_path if scope == "owned-entry" else None)
    assert stat.S_IMODE(target.stat().st_mode) == before
    assert directory.is_symlink()
    if scope == "pending-base":
        assert marker.exists()


def test_permission_repair_refuses_foreign_owned_entry(tmp_path, monkeypatch):
    from bgwcli import recovery_state

    directory = tmp_path / "entry"
    directory.mkdir(mode=0o500)
    before = stat.S_IMODE(directory.stat().st_mode)
    actual_uid = os.geteuid()
    monkeypatch.setattr(recovery_state.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.ensure_directory(directory, boundary=tmp_path)
    assert stat.S_IMODE(directory.stat().st_mode) == before


def test_invalid_pending_marker_cannot_repair_inaccessible_directory(tmp_path):

    directory = tmp_path / "state"
    directory.mkdir()
    marker = filesystem._publication_marker(directory)
    marker.write_text("foreign content")
    marker.chmod(0o600)
    directory.chmod(0)
    try:
        with pytest.raises(PermissionError, match="preserved"):
            filesystem.ensure_directory(directory)
        assert stat.S_IMODE(directory.stat().st_mode) == 0
        assert marker.read_text() == "foreign content"
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("component", ["bgw", "recovery", "custom-root"])
@pytest.mark.parametrize("outside", [False, True])
def test_checkpoint_refuses_application_symlinks_without_target_changes(
    tmp_path, monkeypatch, component, outside,
):
    state = tmp_path / "state"
    state.mkdir()
    target = (tmp_path if outside else state) / "target"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_text("preserve this target")
    target.chmod(0o500)
    before = stat.S_IMODE(target.stat().st_mode)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    if component == "custom-root":
        alias = state / "custom"
        root = alias
    else:
        alias = state / "bgw"
        if component == "recovery":
            alias.mkdir()
            alias = alias / "recovery"
        root = None
    alias.symlink_to(target, target_is_directory=True)
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)

    with pytest.raises(PermissionError, match="symlink.*preserved"):
        store.begin()

    assert alias.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == before
    assert sentinel.read_text() == "preserve this target"
    assert list(target.iterdir()) == [sentinel]
    assert not list(tmp_path.rglob(".bgw-publication-*.pending"))


def test_checkpoint_accepts_state_base_alias(tmp_path, monkeypatch):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(alias))
    store = RecoveryCheckpoint("router.local", make_dump())
    store.begin()
    assert store.is_active()
    assert store.path.parent == physical / "bgw" / "recovery"
    store.finish()


@pytest.mark.parametrize("mode", [
    0o500,
    pytest.param(0, marks=pytest.mark.skipif(
        not hasattr(os, "O_PATH"), reason="mode-000 fallback requires Linux O_PATH",
    )),
])
def test_owner_access_repair_when_nofollow_chmod_is_unsupported(tmp_path, monkeypatch, mode):
    from pathlib import Path


    directory = tmp_path / "entry"
    directory.mkdir()
    directory.chmod(mode)
    original_chmod = Path.chmod

    def unsupported_nofollow(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", unsupported_nofollow)
    try:
        filesystem.restore_owner_access(directory)
        assert stat.S_IMODE(directory.stat().st_mode) == mode | 0o700
    finally:
        original_chmod(directory, 0o700)


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_unsupported_chmod_fallback_preserves_replaced_directory(tmp_path, monkeypatch, replacement):
    from pathlib import Path


    directory = tmp_path / "entry"
    directory.mkdir(mode=0o500)
    original = tmp_path / "original"
    target = tmp_path / "target"
    target.mkdir(mode=0o500)
    original_chmod = Path.chmod

    def replace_before_unsupported(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            path.rename(original)
            if replacement == "symlink":
                path.symlink_to(target, target_is_directory=True)
            else:
                path.mkdir(mode=0o500)
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", replace_before_unsupported)
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.restore_owner_access(directory)
    assert stat.S_IMODE(original.stat().st_mode) == 0o500
    assert stat.S_IMODE(directory.stat().st_mode) == 0o500
    assert stat.S_IMODE(target.stat().st_mode) == 0o500


def test_application_symlink_failure_is_a_checkpoint_error_without_router_writes(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target"
    target.mkdir(mode=0o500)
    (state / "bgw").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    store = RecoveryCheckpoint("router.local", make_dump())
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and result.exit_code == 2
    assert "checkpoint" in result.reason and "symlink" in result.reason
    assert not router.posts
    assert stat.S_IMODE(target.stat().st_mode) == 0o500
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("component", ["bgw", "recovery", "custom-root"])
def test_checkpoint_finish_preserves_matching_record_behind_symlink(tmp_path, monkeypatch, component):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    root = state / "custom" if component == "custom-root" else None
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    store.begin()
    before = store.path.read_bytes()
    alias = state / "bgw" if component == "bgw" else store.path.parent
    target = tmp_path / "relocated"
    alias.rename(target)
    target.chmod(0o700)
    alias.symlink_to(target, target_is_directory=True)
    assert store.is_active()

    with pytest.raises(PermissionError, match="symlink.*preserved"):
        store.finish()

    assert store.path.read_bytes() == before
    assert alias.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Linux O_PATH compatibility fallback")
def test_unsupported_nofollow_checkpoint_survives_restrictive_umask(tmp_path, monkeypatch):
    from pathlib import Path

    original_chmod = Path.chmod

    def unsupported_nofollow(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", unsupported_nofollow)
    store = checkpoint(tmp_path / "new-state")
    previous = os.umask(0o777)
    try:
        store.begin()
        assert store.is_active()
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
        store.finish()
        assert not store.path.exists()
    finally:
        os.umask(previous)


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Linux O_PATH compatibility fallback")
def test_unsupported_chmod_without_procfs_preserves_directory(tmp_path, monkeypatch):

    directory = tmp_path / "entry"
    directory.mkdir(mode=0o500)
    original_chmod = os.chmod

    def unavailable_chmod(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        if os.fspath(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError("procfs unavailable")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", unavailable_chmod)
    with pytest.raises(FileNotFoundError, match="procfs unavailable"):
        filesystem.restore_owner_access(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o500


def test_permission_retry_does_not_republish_existing_child_when_is_dir_suppresses_errors(
    tmp_path, monkeypatch,
):
    from pathlib import Path


    parent = tmp_path / "pending"
    parent.mkdir()
    existing = parent / "existing"
    existing.mkdir()
    marker = filesystem._publication_marker(parent)
    marker.write_bytes(filesystem._marker_content(parent))
    marker.chmod(0o600)
    original_is_dir = Path.is_dir
    original_sync = filesystem.sync_directory
    synchronized = []

    def python314_is_dir(path):
        try:
            return original_is_dir(path)
        except OSError:
            return False

    def record_sync(directory):
        synchronized.append(directory)
        original_sync(directory)

    monkeypatch.setattr(Path, "is_dir", python314_is_dir)
    monkeypatch.setattr(filesystem, "sync_directory", record_sync)
    parent.chmod(0)
    try:
        filesystem.ensure_directory(existing)
        assert synchronized == [tmp_path]
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert not marker.exists()
        assert list(parent.iterdir()) == [existing]
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("fresh_mode", [0o700, 0o750, 0o1711])
def test_unsupported_chmod_accepts_same_inode_permission_updates(tmp_path, monkeypatch, fresh_mode):
    from pathlib import Path


    directory = tmp_path / "entry"
    directory.mkdir(mode=0o500)
    identity = directory.stat().st_ino
    original_chmod = Path.chmod

    def concurrent_repair(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            original_chmod(path, fresh_mode)
            raise NotImplementedError("no-follow chmod unsupported")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", concurrent_repair)
    filesystem.restore_owner_access(directory)
    assert directory.stat().st_ino == identity
    assert stat.S_IMODE(directory.stat().st_mode) == fresh_mode | 0o700


def test_checkpoint_permission_change_preserves_leaf_replaced_by_symlink(tmp_path, monkeypatch):
    from pathlib import Path

    store = checkpoint(tmp_path)
    leaf = store.path.parent
    leaf.mkdir(mode=0o775)
    leaf.chmod(0o775)
    target = tmp_path / "foreign"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("target must survive")
    target.chmod(0o500)
    displaced = tmp_path / "displaced"
    original_chmod = Path.chmod
    swapped = False

    def swap_leaf_before_private_mode(path, mode, *, follow_symlinks=True):
        nonlocal swapped
        if path == leaf and mode == 0o700 and not swapped:
            swapped = True
            path.rename(displaced)
            path.symlink_to(target, target_is_directory=True)
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", swap_leaf_before_private_mode)
    with pytest.raises(OSError):
        store.begin()
    assert swapped
    assert leaf.is_symlink()
    assert stat.S_IMODE(target.stat().st_mode) == 0o500
    assert sentinel.read_text() == "target must survive"
    assert list(target.iterdir()) == [sentinel]


def test_established_application_directories_are_published_once_per_begin(tmp_path, monkeypatch):
    from collections import Counter
    from pathlib import Path

    from bgwcli import recovery_state

    state = tmp_path / "state"
    bgw = state / "bgw"
    recovery = bgw / "recovery"
    recovery.mkdir(parents=True)
    bgw.chmod(0o775)
    recovery.chmod(0o775)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    store = RecoveryCheckpoint("router.local", make_dump())
    identities = {p.stat().st_ino: p for p in (state, bgw, recovery)}
    original_mkdir = Path.mkdir
    original_chmod = Path.chmod
    original_fsync = os.fsync
    mkdir_calls = []
    chmod_calls = []
    directory_syncs = []
    file_syncs = []

    def observe_mkdir(path, *args, **kwargs):
        mkdir_calls.append(path)
        return original_mkdir(path, *args, **kwargs)

    def observe_chmod(path, *args, **kwargs):
        chmod_calls.append(path)
        return original_chmod(path, *args, **kwargs)

    def observe_sync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            directory_syncs.append(identities[info.st_ino])
        else:
            file_syncs.append(info.st_ino)
        return original_fsync(descriptor)

    monkeypatch.setattr(Path, "mkdir", observe_mkdir)
    monkeypatch.setattr(Path, "chmod", observe_chmod)
    monkeypatch.setattr(recovery_state.os, "fsync", observe_sync)
    store.begin()
    assert mkdir_calls == []
    assert chmod_calls == [recovery]
    assert Counter(directory_syncs) == Counter({state: 1, bgw: 1, recovery: 1})
    assert len(file_syncs) == 1
    assert stat.S_IMODE(bgw.stat().st_mode) == 0o775
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o700
    assert store.is_active()

    mkdir_calls.clear()
    chmod_calls.clear()
    directory_syncs.clear()
    file_syncs.clear()
    store.begin()
    assert mkdir_calls == [] and chmod_calls == []
    assert Counter(directory_syncs) == Counter({state: 1, bgw: 1, recovery: 1})
    assert len(file_syncs) == 1
    assert store.is_active()


def test_checkpoint_uses_lexical_application_root_after_removed_alias(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    root = state / "recovery"
    target = tmp_path / "foreign"
    target.mkdir(mode=0o500)
    root.symlink_to(target, target_is_directory=True)
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    root.unlink()
    root.mkdir(mode=0o775)
    store.begin()
    assert store.path.parent == root
    assert store.is_active()
    assert list(target.iterdir()) == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o500


def test_concurrent_fallback_mode_update_keeps_checkpoint_leaf_private(tmp_path, monkeypatch):
    from pathlib import Path

    store = checkpoint(tmp_path)
    leaf = store.path.parent
    leaf.mkdir(mode=0o775)
    original_chmod = Path.chmod

    def concurrent_leaf_chmod(path, mode, *, follow_symlinks=True):
        if path == leaf and not follow_symlinks:
            original_chmod(path, 0o777)
            raise NotImplementedError("no-follow chmod unsupported")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", concurrent_leaf_chmod)
    store.begin()
    assert store.is_active()
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_unsupported_chmod_refuses_changed_descriptor_owner(tmp_path, monkeypatch):
    from pathlib import Path

    from bgwcli import recovery_state

    directory = tmp_path / "entry"
    directory.mkdir(mode=0o500)
    original_chmod = Path.chmod
    original_fstat = os.fstat
    original_open = os.open
    opened_descriptors = []

    def unsupported_nofollow(path, mode, *, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("no-follow chmod unsupported")
        return original_chmod(path, mode, follow_symlinks=follow_symlinks)

    def observe_open(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        opened_descriptors.append(descriptor)
        return descriptor

    def changed_owner(descriptor):
        info = list(original_fstat(descriptor))
        info[4] += 1  # Ownership changes between the pathname and descriptor inspections.
        return os.stat_result(info)

    monkeypatch.setattr(Path, "chmod", unsupported_nofollow)
    monkeypatch.setattr(recovery_state.os, "open", observe_open)
    monkeypatch.setattr(recovery_state.os, "fstat", changed_owner)
    with pytest.raises(PermissionError, match="changed.*preserved"):
        filesystem.restore_owner_access(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o500
    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        original_fstat(opened_descriptors[0])


def test_current_directory_checkpoint_root_is_private(tmp_path, monkeypatch):
    from pathlib import Path

    directory = tmp_path / "state"
    directory.mkdir(mode=0o775)
    directory.chmod(0o775)
    monkeypatch.chdir(directory)
    store = RecoveryCheckpoint("router.local", make_dump(), root=Path("."))
    store.begin()
    assert store.is_active()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.finish()
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "foreign-owner", "file"])
def test_private_boundary_leaf_refuses_unsafe_entries(tmp_path, monkeypatch, kind):
    from bgwcli import recovery_state

    directory = tmp_path / "leaf"
    if kind == "file":
        directory.write_text("preserve this file")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o500)
        directory.symlink_to(target, target_is_directory=True)
    else:
        directory.mkdir(mode=0o775)
        actual_uid = os.geteuid()
        monkeypatch.setattr(recovery_state.os, "geteuid", lambda: actual_uid + 1)
    before = stat.S_IMODE(directory.stat().st_mode)
    with pytest.raises(PermissionError, match="preserved"):
        filesystem.ensure_directory(directory, boundary=directory, private=True)
    assert stat.S_IMODE(directory.stat().st_mode) == before
    if kind == "file":
        assert directory.read_text() == "preserve this file"
    if kind == "symlink":
        assert directory.is_symlink()
        assert list(target.iterdir()) == []


@pytest.mark.parametrize("spelling", ["absolute", "current", "parent", "alias"])
@pytest.mark.parametrize("action", ["begin", "finish"])
def test_filesystem_root_checkpoint_refused_before_directory_mutation(tmp_path, monkeypatch, spelling, action):
    from pathlib import Path


    if spelling == "alias":
        root = tmp_path / "root-alias"
        root.symlink_to(Path("/"), target_is_directory=True)
    elif spelling == "absolute":
        root = Path("/")
    else:
        monkeypatch.chdir(Path("/"))
        root = Path("." if spelling == "current" else "..")

    def unexpected_directory_mutation(*_args, **_kwargs):
        pytest.fail("filesystem root must be rejected before directory preparation")

    monkeypatch.setattr(filesystem, "ensure_directory", unexpected_directory_mutation)
    monkeypatch.setattr(Path, "unlink", unexpected_directory_mutation)
    monkeypatch.setattr(RecoveryCheckpoint, "is_active", lambda _self: True)
    store = RecoveryCheckpoint("router.local", make_dump(), root=root)
    with pytest.raises(PermissionError, match="filesystem root"):
        getattr(store, action)()


@pytest.mark.parametrize("component", ["bgw", "recovery"])
def test_owned_mkdir_interruption_retries_parent_sync_before_router_writes(tmp_path, monkeypatch, component):
    from pathlib import Path


    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    root = state / "bgw" / "recovery"
    interrupted = state / "bgw" if component == "bgw" else root
    original_mkdir = Path.mkdir
    original_sync = filesystem.sync_directory

    def interrupt_mkdir(path, *args, **kwargs):
        original_mkdir(path, *args, **kwargs)
        if path == interrupted:
            raise RuntimeError("interrupted immediately after mkdir")

    with monkeypatch.context() as crash:
        crash.setattr(Path, "mkdir", interrupt_mkdir)
        with pytest.raises(RuntimeError, match="immediately after mkdir"):
            RecoveryCheckpoint("router.local", make_dump()).begin()
    assert interrupted.exists()
    assert not list(tmp_path.rglob(".bgw-publication-*.pending"))
    attempts = []
    reject_sync = True

    def synchronize(directory):
        if directory == interrupted.parent:
            attempts.append(directory)
            if reject_sync:
                raise OSError("unpublished owned entry")
        original_sync(directory)

    monkeypatch.setattr(filesystem, "sync_directory", synchronize)
    for _ in range(2):
        store = RecoveryCheckpoint("router.local", make_dump())
        result, router = run(Fetcher(reset_pages()), store=store)
        assert result.status == "error" and not router.posts
        assert "unpublished owned entry" in result.reason
        assert not store.path.exists()
    assert len(attempts) == 2
    reject_sync = False

    class CheckedRouter(FakeRouter):
        def post_cgi_page(self, page, fields):
            assert len(attempts) == 3, "interrupted directory publication must precede router writes"
            return super().post_cgi_page(page, fields)

    router = CheckedRouter()
    store = RecoveryCheckpoint("router.local", make_dump())
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=1),
        fetch_pages=Fetcher(reset_pages(), full_pages()), checkpoint=store, log=lambda _: None,
    )
    assert result.status == "converged" and router.posts


@pytest.mark.parametrize("replacement", ["directory", "symlink", "file"])
def test_missing_owned_entry_is_validated_after_concurrent_creation(tmp_path, monkeypatch, replacement):
    from pathlib import Path

    store = checkpoint(tmp_path)
    leaf = store.path.parent
    target = tmp_path / "target"
    target.mkdir(mode=0o500)
    original_mkdir = Path.mkdir
    created = False

    def concurrent_create(path, *args, **kwargs):
        nonlocal created
        if path == leaf and not created:
            created = True
            if replacement == "directory":
                original_mkdir(path, mode=0o500)
            elif replacement == "symlink":
                path.symlink_to(target, target_is_directory=True)
            else:
                path.write_text("concurrent file")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", concurrent_create)
    if replacement == "directory":
        store.begin()
        assert store.is_active()
        assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    else:
        with pytest.raises(PermissionError, match="preserved"):
            store.begin()
        if replacement == "symlink":
            assert leaf.is_symlink()
        else:
            assert leaf.read_text() == "concurrent file"
    assert created
    assert stat.S_IMODE(target.stat().st_mode) == 0o500
    assert list(target.iterdir()) == []


def test_filesystem_root_checkpoint_failure_is_structured_without_router_writes(monkeypatch):
    from pathlib import Path


    def unexpected_directory_preparation(*_args, **_kwargs):
        pytest.fail("filesystem root must not reach directory preparation")

    monkeypatch.setattr(filesystem, "ensure_directory", unexpected_directory_preparation)
    store = RecoveryCheckpoint("router.local", make_dump(), root=Path("/"))
    result, router = run(Fetcher(reset_pages()), store=store)
    assert result.status == "error" and result.exit_code == 2 and not router.posts
    assert "checkpoint begin failed" in result.reason and "filesystem root" in result.reason


def test_begin_refuses_to_replace_another_recovery_intent(tmp_path):
    """One router has one intent file: a run for a different dump or page selection must not
    overwrite (and so silently cancel) an unfinished recovery it does not own."""
    from bgwcli.recovery_state import RecoveryIntentConflictError

    original = checkpoint(tmp_path)
    original.begin()
    contents = original.path.read_bytes()
    other = checkpoint(tmp_path, dump=replace(make_dump(), forms={"dosprotect": {"flood_protect": "off"}}))
    with pytest.raises(RecoveryIntentConflictError, match="another unfinished recovery"):
        other.begin()
    assert original.path.read_bytes() == contents
    checkpoint(tmp_path).begin()  # re-beginning the same intent stays allowed
    assert original.path.read_bytes() == contents


def test_autorestore_with_another_unfinished_recovery_is_an_error_without_writes(tmp_path):
    checkpoint(tmp_path, pages=("services",)).begin()
    result, router = run(Fetcher(reset_pages()), store=checkpoint(tmp_path))
    assert result.status == "error" and result.exit_code == 2 and not router.posts
    assert "another unfinished recovery" in result.reason
    assert checkpoint(tmp_path, pages=("services",)).is_active()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_unreadable_intent_record_is_a_fault_not_no_intent(tmp_path):
    store = checkpoint(tmp_path)
    store.begin()
    store.path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            store.is_active()
        result, router = run(Fetcher(full_pages()), store=store)
    finally:
        store.path.chmod(0o600)
    assert result.status == "error" and result.exit_code == 2 and not router.posts
    assert "recovery checkpoint" in result.reason


# --- cross-run retry limit ----------------------------------------------------------------------


def _ack_but_never_changes(store, *, fetcher=None, max_passes=1):
    router = FakeRouter()
    result = run_autorestore(
        lambda: (router, False), make_dump(), AutorestoreOptions(commit=True, max_passes=max_passes, wait_seconds=1),
        fetch_pages=fetcher or Fetcher(reset_pages()), checkpoint=store, log=lambda _: None, sleep=lambda _: None,
    )
    return result, router


def test_three_identical_failed_runs_stop_automatic_recovery_with_exit_2(tmp_path):
    outcomes = []
    for _ in range(3):
        result, router = _ack_but_never_changes(checkpoint(tmp_path))
        outcomes.append((result.status, result.exit_code, bool(router.posts)))
    assert outcomes == [("not-converged", 1, True), ("not-converged", 1, True), ("error", 2, True)]
    assert "3 consecutive runs" in result.reason
    record = json.loads(checkpoint(tmp_path).path.read_text())
    assert record["failures"]["count"] == 3 and len(record["failures"]["fingerprint"]) == 64
    assert checkpoint(tmp_path).is_active()

    fourth, router = _ack_but_never_changes(checkpoint(tmp_path))
    assert (fourth.status, fourth.exit_code) == ("error", 2)
    assert router.posts == [], "a stopped recovery never writes again on its own"
    assert "automatic recovery stopped" in fourth.reason and str(checkpoint(tmp_path).path) in fourth.reason


def test_not_converged_reason_says_the_next_timer_run_retries(tmp_path):
    result, _ = _ack_but_never_changes(checkpoint(tmp_path))
    assert "the next timer run will retry" in result.reason
    assert "1/3" in result.reason


def test_a_different_failure_restarts_the_count_and_convergence_clears_it(tmp_path):
    partial = reset_pages()
    partial["services"] = services_page()
    for _ in range(2):
        assert _ack_but_never_changes(checkpoint(tmp_path))[0].exit_code == 1
    # A run that fails differently (services now present, the rest still missing) is failure 1 again.
    result, _ = _ack_but_never_changes(checkpoint(tmp_path), fetcher=Fetcher(partial))
    assert (result.status, result.exit_code) == ("not-converged", 1)
    assert json.loads(checkpoint(tmp_path).path.read_text())["failures"]["count"] == 1
    result, _ = _ack_but_never_changes(checkpoint(tmp_path), fetcher=Fetcher(partial, full_pages()))
    assert result.status == "converged" and not checkpoint(tmp_path).path.exists()


def test_failure_record_survives_the_next_begin_and_keeps_only_hashes(tmp_path):
    dump = replace(make_dump(), forms={**make_dump().forms, "wconfig": {"wpa_key": "test-private-secret"}})
    store = checkpoint(tmp_path, dump=dump)
    store.begin()
    assert store.record_failure("a" * 64) == 1
    store.begin()
    assert store.record_failure("a" * 64) == 2
    assert store.record_failure("b" * 64) == 1
    assert store.is_active()
    assert "test-private-secret" not in store.path.read_text()
    assert checkpoint(tmp_path, dump=make_dump()).record_failure("c" * 64) == 0, "no intent of its own"


def test_unresolved_clear_and_rescan_conflicts_stop_after_three_identical_runs(tmp_path, monkeypatch):
    from bgwcli import autorestore
    from bgwcli.allocation_preflight import AllocationConflict, AllocationPreflight
    from bgwcli.errors import UsageError

    conflict = AllocationConflict("192.168.1.64", "02:0a:0b:0c:0d:02", "aa:bb:cc:dd:ee:09", "holder", "on", False)
    monkeypatch.setattr(autorestore, "inspect_allocation_conflicts", lambda *_args, **_kw: AllocationPreflight(
        [conflict], {"Clear": "Clear and Rescan for Devices"},
    ))

    def rescan(client, preflight, *, log, evidence):
        evidence.clear_attempted = evidence.clear_response_received = evidence.clear_accepted = True
        client.post_cgi_page("devices", preflight.clear_payload)
        raise UsageError("IP allocation conflict after Clear and Rescan: 192.168.1.64 is still allocated")

    monkeypatch.setattr(autorestore, "rescan_allocation_conflicts", rescan)
    clears = []
    for _ in range(4):
        router = FakeRouter()
        result = run_autorestore(
            lambda router=router: (router, False), make_dump(), AutorestoreOptions(commit=True),
            fetch_pages=Fetcher(reset_pages()), checkpoint=checkpoint(tmp_path), log=lambda _: None,
        )
        clears.append(len(router.posts))
        assert (result.status, result.exit_code) == ("error", 2)
    assert clears == [1, 1, 1, 0], "the fourth run must not post Clear and Rescan again"
    assert "automatic recovery stopped" in result.reason
