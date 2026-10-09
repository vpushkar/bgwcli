"""Map exceptions to process exit codes."""

from .errors import SessionLockTimeoutError, UsageError

NEGATIVE_ANSWER = 1
NO_ANSWER = 2
INTERRUPTED = 130  # the shell's 128 + SIGINT for a run ended by Ctrl-C

# The only raised errors that are an answer: bad usage, and lock contention timeouts (kept at exit 1
# for compatibility). Every other exception (transport, HTTP status, auth, lock, filesystem, unknown
# write state, and unexpected faults) means the command could not answer.
_NEGATIVE = (UsageError, SessionLockTimeoutError)


def fatal_exit_code(error: BaseException) -> int:
    """Exit 1 for usage errors and lock timeouts; exit 2 for everything else (no answer)."""
    return NEGATIVE_ANSWER if isinstance(error, _NEGATIVE) else NO_ANSWER
