"""Exception hierarchy shared by every module.

Exit-code contract (see exit_codes.py): exit 1 means "the command ran and the answer was
negative" (diff found differences, restore did not converge); exit 2 means the command could
not produce an answer at all. Every exception below that means "no answer" maps to 2.
"""


class BgwError(Exception):
    """Base class for all bgwcli errors."""


class RouterConnectionError(BgwError):
    """The gateway could not be reached or returned a transport-level failure."""


class RouterAuthError(BgwError):
    """Login failed or the router answered with its login page."""


class RouterSessionPoolFullError(RouterAuthError):
    """The gateway's small web-session pool is exhausted.

    Subclasses RouterAuthError exactly like the TS `RouterSessionPoolFullError extends RouterAuthError`,
    so every `except RouterAuthError: raise` re-raises pool-full too. Sites that need to tell the two
    apart (cli.main, sweep._safe_sweep_page) must catch the pool-full class FIRST.
    """


class DumpFileError(BgwError):
    """A dump file is missing, unreadable, or structurally invalid."""


class SnapshotExtractionError(BgwError):
    """A live page could not be turned into a Snapshot safely."""


class UsageError(BgwError):
    """Bad command-line usage."""
