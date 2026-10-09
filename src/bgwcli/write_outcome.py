"""The one rule for a configuration write the gateway never answered.

restore, autorestore and the CLI all report `writeUnanswered` from this predicate, so the three
cannot disagree about the same step.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar

# The prefix of a step error that carries the gateway's own rejection banner (an answer).
WRITE_REJECTED_PREFIX = "router rejected the change: "


class _WriteEvidence(Protocol):
    status: str
    error: str | None
    write_attempted: bool | None
    write_response_received: bool | None
    write_performed: bool | None
    acknowledgement_observed: bool | None


_StepT = TypeVar("_StepT", bound=_WriteEvidence)


def unanswered_write(step: _WriteEvidence) -> bool:
    """True for a failed step whose configuration POST was sent (or may have been: only an explicit
    `write_attempted is False` rules that out) and got no answer, and for a reconnect-required step
    whose POST got no response at all: a transport fault, an HTTP
    error, a refused session, a full session pool, or a reply that never carried `Changes saved`.
    An explicit rejection banner, an observed `Changes saved` or `No changes detected` answer and
    any step that sent nothing (including a session pool that was full before the write) are not
    unanswered; the closing diff decides those."""
    if step.status == "reconnect-required":
        # A LAN move normally ends here: the gateway answered and left the old address. Only a POST
        # that got no response is a write of unknown outcome.
        if step.write_response_received is True:
            return False
    elif step.status != "failed":
        return False
    if step.write_attempted is False:
        return False
    if step.write_performed is False or step.acknowledgement_observed is True:
        return False
    return not (step.error or "").startswith(WRITE_REJECTED_PREFIX)


def unanswered_write_step(steps: Iterable[_StepT]) -> _StepT | None:
    """The first step for which `unanswered_write` holds, or None."""
    return next((step for step in steps if unanswered_write(step)), None)
