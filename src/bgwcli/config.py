"""Global options: environment defaults and access-code resolution.

Environment contract (identical to the TypeScript CLI so both can share one setup):
BGW_HOST / ROUTER_IP, BGW_ACCESS_CODE, BGW_TIMEOUT_MS, BGW_INSECURE_TLS ("0" disables),
BGW_WAIT_FOR_SESSION ("1" enables), BGW_SESSION_WAIT_TIMEOUT_MS, BGW_SESSION_WAIT_INTERVAL_MS,
BGW_SESSION_CACHE_TTL_MS, BGW_SESSION_POOL_COOLDOWN_MS, BGW_SESSION_LOCK_TIMEOUT_MS.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .errors import UsageError

DEFAULT_HOST = "192.168.1.254"


@dataclass
class GlobalOptions:
    host: str = DEFAULT_HOST
    access_code: str | None = None
    access_code_stdin: bool = False
    json: bool = False
    include_secrets: bool = False
    timeout_ms: int = 15000
    timeout_explicit: bool = False  # set by --timeout or BGW_TIMEOUT_MS; disables slow-page floors
    insecure_tls: bool = True
    wait_for_session: bool = False
    session_wait_timeout_ms: int = 120000
    session_wait_interval_ms: int = 10000
    session_cache_ttl_ms: int = 120000
    session_pool_cooldown_ms: int = 300000
    session_lock_timeout_ms: int = 300000


def env_number(name: str, fallback: int, minimum: int) -> int | float:
    """Numeric env var with JS `Number()` semantics for the documented cases.

    Accepted like TS: decimal integers, fractional values (kept as a float, e.g. `1500.5`),
    exponent notation (`1e3`) and surrounding whitespace. Integral values come back as `int` so
    JSON output and arithmetic stay identical to the TS CLI. Rejected like TS: empty -> fallback,
    NaN/Infinity/non-numeric/below-minimum -> UsageError with the TS message.
    Known divergences (undocumented inputs): JS `Number("0x10")` is 16 but Python rejects hex, and
    Python accepts `1_000` where JS yields NaN.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    try:
        value = float(raw)
    except ValueError as exc:
        raise UsageError(f"{name} must be a finite number greater than or equal to {minimum}.") from exc
    if value != value or value in (float("inf"), float("-inf")) or value < minimum:
        raise UsageError(f"{name} must be a finite number greater than or equal to {minimum}.")
    return int(value) if value.is_integer() else value


def env_default_options() -> GlobalOptions:
    return GlobalOptions(
        host=os.environ.get("BGW_HOST") or os.environ.get("ROUTER_IP") or DEFAULT_HOST,
        timeout_ms=env_number("BGW_TIMEOUT_MS", 15000, 1),
        timeout_explicit=bool(os.environ.get("BGW_TIMEOUT_MS")),
        insecure_tls=os.environ.get("BGW_INSECURE_TLS") != "0",
        wait_for_session=os.environ.get("BGW_WAIT_FOR_SESSION") == "1",
        session_wait_timeout_ms=env_number("BGW_SESSION_WAIT_TIMEOUT_MS", 120000, 0),
        session_wait_interval_ms=env_number("BGW_SESSION_WAIT_INTERVAL_MS", 10000, 1),
        session_cache_ttl_ms=env_number("BGW_SESSION_CACHE_TTL_MS", 120000, 0),
        session_pool_cooldown_ms=env_number("BGW_SESSION_POOL_COOLDOWN_MS", 300000, 0),
        session_lock_timeout_ms=env_number("BGW_SESSION_LOCK_TIMEOUT_MS", 300000, 1),
    )


def resolve_access_code(options: GlobalOptions, stdin=None) -> str | None:
    """Explicit option, then BGW_ACCESS_CODE, then stdin when --access-code-stdin was given."""
    if options.access_code:
        return options.access_code
    env = os.environ.get("BGW_ACCESS_CODE")
    if env:
        return env
    if not options.access_code_stdin:
        return None
    stream = stdin if stdin is not None else sys.stdin
    # TS `trimEnd()`: strip every trailing whitespace character, keep leading whitespace.
    return stream.read().rstrip()
