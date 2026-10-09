"""Exception hierarchy shared by every module.

Exit-code contract (see exit_codes.py): exit 1 means "the command ran and the answer was
negative" (diff found differences, restore did not converge); exit 2 means the command could
not produce an answer at all. Only UsageError and SessionLockTimeoutError (kept for compatibility)
map to 1; every other exception, including unexpected non-bgwcli faults, maps to 2.
"""


class BgwError(Exception):
    """Base class for all bgwcli errors."""


class SessionLockError(BgwError):
    """Could not perform a local router session locking operation."""


class SessionLockTimeoutError(SessionLockError):
    """Timed out waiting for the local router session lock (plain Error in TS: exit 1)."""


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


def is_page_level_auth_error(error: BaseException) -> bool:
    """True for a 401/403 answered by one content page: BGW320Client marks that RouterAuthError
    ``page_level=True`` at the raise site. That page is unavailable; the session is not known to be
    lost, so per-page readers (sweep, page, parsed-page fetch) record it and continue. The login
    handshake also attaches a status code on 401/403 but never sets the marker: a failed login, a
    login-page bounce and pool-full stay session-wide (callers re-raise and abort)."""
    return (
        isinstance(error, RouterAuthError)
        and not isinstance(error, RouterSessionPoolFullError)
        and getattr(error, "page_level", False) is True
    )


class DumpFileError(BgwError):
    """A dump file is missing, unreadable, or structurally invalid."""


class SnapshotExtractionError(BgwError):
    """A live page could not be turned into a Snapshot safely."""


class WriteUnconfirmedError(BgwError):
    """A configuration write was sent once but the gateway's acknowledgement was never observed.

    The router state is unknown, so this is "no answer" (exit 2), matching the unconfirmed
    outcome the Wi-Fi and LAN save paths report; it is neither a negative answer nor bad usage.
    """


class UsageError(BgwError):
    """Bad command-line usage."""
