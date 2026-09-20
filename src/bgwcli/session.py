"""Local router-session coordination (port of BGW320-CLI src/session.ts).

Files (shared with the TypeScript CLI, same paths and JSON):
  <root>/<sha256(origin)[:24]>.session.json   {"origin","authenticated","cookies","version":1,"cachedAt","expiresAt"}
  <root>/<key>.cooldown.json                   {"version":1,"origin","until","waitedMs","retryCount"}
  <root>/<key>.lock                            {"pid","createdAt"}   (O_CREAT|O_EXCL; stale by mtime)
root = $BGW_SESSION_CACHE_DIR, else $XDG_CACHE_HOME/bgw, else ~/.cache/bgw. Timestamps are epoch ms.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from .client import BGW320Client, normalize_origin, pool_full_metadata, session_pool_full_error
from .config import GlobalOptions
from .errors import BgwError, RouterSessionPoolFullError

T = TypeVar("T")
_DEFAULT_LOCK_STALE_MS = 900000
_sleep = time.sleep


class SessionLockTimeoutError(BgwError):
    """Timed out waiting for the local router session lock (plain Error in TS: exit 1)."""


@dataclass(frozen=True)
class SessionCoordinatorOptions:
    cache_ttl_ms: int
    pool_cooldown_ms: int
    lock_timeout_ms: int
    wait_for_session: bool

    @classmethod
    def from_options(cls, options: GlobalOptions) -> SessionCoordinatorOptions:
        return cls(
            cache_ttl_ms=options.session_cache_ttl_ms,
            pool_cooldown_ms=options.session_pool_cooldown_ms,
            lock_timeout_ms=options.session_lock_timeout_ms,
            wait_for_session=options.wait_for_session,
        )


@dataclass(frozen=True)
class SessionState:
    cached: bool
    cache_expires_at: int | None = None
    pool_cooldown_until: int | None = None


@dataclass(frozen=True)
class SessionPaths:
    origin: str
    cache: Path
    cooldown: Path
    lock: Path


def router_session_identity(host: str) -> str:
    return normalize_origin(host)


def session_paths(origin: str) -> SessionPaths:
    key = hashlib.sha256(origin.encode()).hexdigest()[:24]
    env_root = os.environ.get("BGW_SESSION_CACHE_DIR")
    if env_root:
        root = Path(env_root)
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg else Path(_home_or_tmp()) / ".cache"
        root = base / "bgw"
    return SessionPaths(
        origin=origin,
        cache=root / f"{key}.session.json",
        cooldown=root / f"{key}.cooldown.json",
        lock=root / f"{key}.lock",
    )


def with_router_session(client: BGW320Client, options: SessionCoordinatorOptions, run: Callable[[], T]) -> T:
    """Load cached session -> run -> persist; pool-full results/errors start a local cooldown."""
    paths = session_paths(client.session_identity())
    _ensure_private_dir(paths.cache.parent)
    release = _acquire_lock(paths.lock, options.lock_timeout_ms)
    try:
        _apply_cooldown(paths.cooldown)
        cached = _read_json(paths.cache)
        if cached is not None and _expires_at(cached) > _now_ms():
            client.import_session(cached)

        result = run()
        if _contains_session_pool_full(result):
            _remember_session_pool_full(paths, session_pool_full_error(), options.pool_cooldown_ms)
            client.clear_session()
            return result

        if client.has_authenticated_session():
            snapshot = client.export_session()
            now = _now_ms()
            _write_json(
                paths.cache,
                {
                    "origin": snapshot.origin,
                    "authenticated": snapshot.authenticated,
                    "cookies": dict(snapshot.cookies),
                    "version": 1,
                    "cachedAt": now,
                    "expiresAt": now + max(0, options.cache_ttl_ms),
                },
            )
            paths.cooldown.unlink(missing_ok=True)
        return result
    except RouterSessionPoolFullError as error:
        _remember_session_pool_full(paths, error, options.pool_cooldown_ms)
        client.clear_session()
        raise
    finally:
        release()


def read_session_state(origin: str) -> SessionState:
    paths = session_paths(origin)
    cached = _read_json(paths.cache)
    cooldown = _read_json(paths.cooldown)
    now = _now_ms()
    cache_live = cached is not None and _expires_at(cached) > now
    cooldown_live = cooldown is not None and _int_field(cooldown, "until") > now
    return SessionState(
        cached=cache_live,
        cache_expires_at=_expires_at(cached) if cache_live else None,
        pool_cooldown_until=_int_field(cooldown, "until") if cooldown_live else None,
    )


def clear_session_state(origin: str, lock_timeout_ms: int = 300000) -> None:
    paths = session_paths(origin)
    _ensure_private_dir(paths.lock.parent)
    release = _acquire_lock(paths.lock, lock_timeout_ms)
    try:
        paths.cache.unlink(missing_ok=True)
        paths.cooldown.unlink(missing_ok=True)
    finally:
        release()


# --- internals -------------------------------------------------------------------------------


def _apply_cooldown(path: Path) -> None:
    cooldown = _read_json(path)
    if cooldown is None:
        return
    if _int_field(cooldown, "until") <= _now_ms():
        path.unlink(missing_ok=True)
        return
    raise session_pool_full_error(
        "Router web session pool is full; local cooldown is active.",
        waited_ms=_int_field(cooldown, "waitedMs"),
        retry_count=_int_field(cooldown, "retryCount"),
    )


def _remember_session_pool_full(paths: SessionPaths, error: BaseException, pool_cooldown_ms: int = 300000) -> None:
    waited_ms, retry_count = pool_full_metadata(error)
    paths.cache.unlink(missing_ok=True)
    _write_json(
        paths.cooldown,
        {
            "version": 1,
            "origin": paths.origin,
            "until": _now_ms() + max(0, pool_cooldown_ms),
            "waitedMs": waited_ms,
            "retryCount": retry_count,
        },
    )


def _contains_session_pool_full(value: Any) -> bool:
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return False
    if isinstance(value, dict):
        return value.get("sessionPoolFull") is True or value.get("session_pool_full") is True
    if isinstance(value, (list, tuple)):
        return any(_contains_session_pool_full(item) for item in value)
    return getattr(value, "session_pool_full", None) is True


def _acquire_lock(path: Path, timeout_ms: int) -> Callable[[], None]:
    started = _now_ms()
    stale_ms = _lock_stale_ms()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            _remove_stale_lock(path, stale_ms)
            if _now_ms() - started >= timeout_ms:
                raise SessionLockTimeoutError("Timed out waiting for local router session lock.") from error
            _sleep(0.1)
            continue
        try:
            os.write(fd, _dumps({"pid": os.getpid(), "createdAt": _now_ms()}).encode())
        finally:
            os.close(fd)

        def release() -> None:
            path.unlink(missing_ok=True)

        return release


def _lock_stale_ms() -> float:
    raw = os.environ.get("BGW_SESSION_LOCK_STALE_MS") or str(_DEFAULT_LOCK_STALE_MS)
    try:
        return float(raw)
    except ValueError:
        return float("inf")  # Number(...) -> NaN in TS: the comparison is never true


def _remove_stale_lock(path: Path, stale_ms: float) -> None:
    try:
        mtime_ms = path.stat().st_mtime * 1000
    except FileNotFoundError:
        return
    if _now_ms() - mtime_ms > stale_ms:
        path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not _is_regular_file(info):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, (_dumps(value) + "\n").encode())
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)
        raise


def _ensure_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)


def _dumps(value: Any) -> str:
    """JSON.stringify(): compact separators, key order preserved."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _expires_at(cached: dict[str, Any] | None) -> int:
    return _int_field(cached, "expiresAt")


def _int_field(record: dict[str, Any] | None, key: str) -> int:
    if not record:
        return 0
    value = record.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _is_regular_file(info: os.stat_result) -> bool:
    import stat

    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _home_or_tmp() -> str:
    try:
        return str(Path.home())
    except (RuntimeError, KeyError):
        return tempfile.gettempdir()


def _now_ms() -> int:
    return int(time.time() * 1000)
