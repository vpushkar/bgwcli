"""The shared unanswered-write predicate."""

from __future__ import annotations

from bgwcli import restore
from bgwcli.restore import RestoreStepResult
from bgwcli.write_outcome import WRITE_REJECTED_PREFIX, unanswered_write, unanswered_write_step


def step(**fields) -> RestoreStepResult:
    base = {"order": 1, "page": "p", "kind": "form", "description": "d", "status": "failed"}
    return RestoreStepResult(**{**base, **fields})


def test_prefix_matches_the_one_restore_writes():
    assert WRITE_REJECTED_PREFIX == restore.WRITE_REJECTED_PREFIX


def test_sent_or_possibly_sent_failures_without_an_answer_are_unanswered():
    assert unanswered_write(step(write_attempted=True, error="timed out"))
    assert unanswered_write(step(write_attempted=None, error="lost"))
    assert unanswered_write(step(write_attempted=True, session_pool_full=True, error="full"))
    assert unanswered_write(step(write_attempted=True, write_response_received=True, status_code=200))


def test_answers_and_unsent_writes_are_not_unanswered():
    assert not unanswered_write(step(write_attempted=False, error="nonce"))
    assert not unanswered_write(step(write_attempted=False, session_pool_full=True, error="full before the write"))
    assert not unanswered_write(step(write_attempted=True, error=f"{WRITE_REJECTED_PREFIX}bad"))
    assert not unanswered_write(step(write_attempted=True, write_performed=False))
    assert not unanswered_write(step(write_attempted=True, acknowledgement_observed=True))
    assert not unanswered_write(step(status="applied", write_attempted=True))
    assert not unanswered_write(step(status="reconnect-required", write_attempted=False))
    assert not unanswered_write(step(status="reconnect-required", write_attempted=True, write_response_received=True))
    assert not unanswered_write(step(status="reconnect-required", write_attempted=True, acknowledgement_observed=True))


def test_a_reconnect_required_step_whose_post_got_no_response_is_unanswered():
    assert unanswered_write(step(status="reconnect-required", write_attempted=True, write_response_received=False))
    assert unanswered_write(step(status="reconnect-required", write_attempted=None, write_response_received=False))
    assert unanswered_write(step(status="reconnect-required", write_attempted=True, write_response_received=False,
                                 error="timed out"))


def test_first_unanswered_step_is_returned():
    lost = step(write_attempted=True, error="lost")
    assert unanswered_write_step([step(status="applied", write_attempted=True), lost]) is lost
    assert unanswered_write_step([step(write_attempted=False)]) is None
