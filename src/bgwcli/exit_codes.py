"""Map exceptions to process exit codes."""

from .errors import (
    DumpFileError,
    RouterAuthError,
    RouterConnectionError,
    RouterSessionPoolFullError,
    SnapshotExtractionError,
)

NEGATIVE_ANSWER = 1
NO_ANSWER = 2

_FATAL = (RouterSessionPoolFullError, DumpFileError, SnapshotExtractionError, RouterAuthError, RouterConnectionError)


def fatal_exit_code(error: BaseException) -> int:
    """Exit 2 for errors that mean the command could not answer; otherwise 1."""
    return NO_ANSWER if isinstance(error, _FATAL) else NEGATIVE_ANSWER
