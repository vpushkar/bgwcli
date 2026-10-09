"""autorestore mirrors restore's terminal rules: no closing read after a lost session, a pre-send fault
is router-unreachable, and an acknowledged write whose state read fails is verification-unavailable."""

from __future__ import annotations

from test_autorestore import (
    FakeRouter,
    Fetcher,
    _canned_execution,
    _no_sleep,
    full_pages,
    make_dump,
    reset_pages,
)

from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.errors import RouterAuthError
from bgwcli.recovery_state import RecoveryCheckpoint


def test_a_session_wide_auth_failure_on_the_write_step_makes_no_closing_read():
    class Router(FakeRouter):
        def post_cgi_page(self, page, fields):
            raise RouterAuthError("Router returned the login page instead of accepting the operation.")

    fetcher = Fetcher(reset_pages(), full_pages())
    result = run_autorestore(
        lambda: (Router(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, pages=("services",)),
        fetch_pages=fetcher, sleep=_no_sleep, log=lambda _: None,
    )
    assert len(fetcher.calls) == 1
    assert result.status == "error" and result.exit_code == 2 and result.write_unanswered is True
    assert len(result.passes) == 1


def test_a_pre_send_transport_fault_ends_the_run_unreachable_without_counting(monkeypatch, tmp_path):
    _canned_execution(monkeypatch, write_attempted=False, error_type="RouterConnectionError", error="nonce timed out")
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=fetcher, sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "router-unreachable" and result.exit_code == 0
    assert len(result.passes) == 1 and sleeps == [] and len(fetcher.calls) == 1
    assert result.write_unanswered is False and store.failure_count() == 0


def test_an_acknowledged_write_whose_state_read_fails_is_verification_unavailable_and_counts(monkeypatch, tmp_path):
    _canned_execution(
        monkeypatch, write_attempted=True, write_response_received=True, write_performed=True,
        acknowledgement_observed=True, error_type="RouterResponseError", error="verification read returned HTTP 500",
    )
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    sleeps: list[float] = []
    result = run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=Fetcher(reset_pages()), sleep=sleeps.append, log=lambda _: None, checkpoint=store,
    )
    assert result.status == "error" and result.exit_code == 2
    assert "verification-unavailable" in result.reason and "acknowledged" in result.reason
    assert result.write_unanswered is False and len(result.passes) == 1 and sleeps == []
    assert store.failure_count() == 1
    step = result.passes[0]["steps"][0]
    assert step["writeAttempted"] is True and step["acknowledgementObserved"] is True
