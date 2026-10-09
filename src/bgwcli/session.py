"""Local router-session coordination (port of BGW320-CLI src/session.ts).

Files (session/cache JSON shared with the TypeScript CLI; lock ownership is Python-specific):
  <root>/<sha256(origin)[:24]>.session.json   {"origin","authenticated","cookies","version":1,"cachedAt","expiresAt"}
  <root>/<key>.cooldown.json                   {"version":1,"origin","until","waitedMs","retryCount"}
  <root>/<key>.lock                            {"pid","createdAt","token","ownership"} (lifetime flock)
  <root>/<key>.lock.guard                      permanent advisory mutex for marker ownership changes
root = $BGW_SESSION_CACHE_DIR, else $XDG_CACHE_HOME/bgw, else ~/.cache/bgw. Timestamps are epoch ms.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from . import filesystem
from .client import BGW320Client, normalize_origin, pool_full_metadata, session_pool_full_error
from .config import GlobalOptions, env_number
from .errors import (
    BgwError,
    RouterAuthError,
    RouterSessionPoolFullError,
    SessionLockError,
    SessionLockTimeoutError,
    is_page_level_auth_error,
)

T = TypeVar("T")
_DEFAULT_LOCK_STALE_MS = 900000
_LOCK_OWNERSHIP = "flock-v1"
_LOCK_RELEASE_TIMEOUT_SECONDS = 1.0
_sleep = time.sleep


def _warn(message: str) -> None:
    # Look up sys.stderr at call time so redirected or captured streams receive the warning.
    # Best-effort: every caller is reporting an ancillary cache failure after the router work is
    # done, so a broken or momentarily unwritable stderr must not replace that completed outcome.
    with suppress(OSError):
        sys.stderr.write(f"warning: {message}\n")


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
    # Capture relative configuration before a callback can change the working directory.
    root = root.absolute()
    return SessionPaths(
        origin=origin,
        cache=root / f"{key}.session.json",
        cooldown=root / f"{key}.cooldown.json",
        lock=root / f"{key}.lock",
    )


def with_router_session(client: BGW320Client, options: SessionCoordinatorOptions, run: Callable[[], T]) -> T:
    """Load cached session -> run -> persist; pool-full results/errors start a local cooldown.

    A successful run persists (and so re-stamps) the session only when it was used or replaced:
    the run made an authenticated router request, logged in, or ends holding cookies other than
    the cached ones. A run that never touched the router leaves the cached lifetime alone.
    A session obtained during a failing ``run`` is still persisted, so the next command reuses it;
    an imported session the failing run did not replace keeps its recorded lifetime. A
    session-wide authentication error drops the cached session instead; pool-full records a
    cooldown and drops it too. A run that returns normally but ends without an authenticated
    session (lost and not re-established) drops the cached session as well."""
    paths = session_paths(client.session_identity())
    _ensure_private_dir(paths.cache.parent)
    release = _acquire_lock(paths.lock, options.lock_timeout_ms)
    try:
        _apply_cooldown(paths.cooldown, options.pool_cooldown_ms)
    except BaseException:
        release()
        raise
    try:
        cached = _read_cached_session(paths.cache)
        stored_cookies: dict[str, str] | None = None
        if _cache_live(cached, _now_ms(), options.cache_ttl_ms):
            client.import_session(cached)
            stored_cookies = _session_cookies(client)
        # What the client held before run(): a failing run persists only a session it obtained itself.
        initial_cookies = _session_cookies(client)
        usage_before = _session_usage(client)

        try:
            result = run()
        except RouterSessionPoolFullError:
            raise
        except RouterAuthError as error:
            if not is_page_level_auth_error(error):
                # The gateway bounced or refused the session: a cached copy is dead and would only be
                # imported (and bounced again) by every run until it expires.
                client.clear_session()
                _forget_cached_session(paths)
            elif _session_cookies(client) != initial_cookies:
                _persist_session(client, paths, options)
            raise
        except BaseException:
            # A rejected save, an unconfirmed write or a snapshot fault after a fresh login still
            # leaves a live router session; dropping it would make the next run spend another slot of
            # the small pool. An imported session this run never replaced was not verified by a
            # failing run, so its recorded lifetime is left alone.
            if initial_cookies is not None and not client.has_authenticated_session():
                # The run lost the imported session and did not replace it: the cached copy is dead.
                _forget_cached_session(paths)
            elif _session_cookies(client) != initial_cookies:
                _persist_session(client, paths, options)
            raise
        pool_metadata = _result_pool_full_metadata(result)
        if pool_metadata is not None:
            waited_ms, retry_count = pool_metadata
            error = session_pool_full_error(waited_ms=waited_ms, retry_count=retry_count)
            client.clear_session()
            _remember_session_pool_full(paths, error, options.pool_cooldown_ms)
            return result

        if not client.has_authenticated_session():
            # The run ended logged out (e.g. a lost session whose re-login the gateway refused inside a
            # handled re-read): any cached copy is dead and would only be imported and bounced again.
            _forget_cached_session(paths)
            return result
        if _session_usage(client) != usage_before or _session_cookies(client) != stored_cookies:
            _persist_session(client, paths, options)
        return result
    except RouterSessionPoolFullError as error:
        client.clear_session()
        _remember_session_pool_full(paths, error, options.pool_cooldown_ms)
        raise
    finally:
        release()


def read_session_state(
    origin: str, *, cache_ttl_ms: int | None = None, pool_cooldown_ms: int | None = None
) -> SessionState:
    """The local cache/cooldown view, with the coordinator's wall-clock sanity cap: a cache stamped
    in the future, or (given the configured lengths) an unstamped cache or a cooldown further ahead
    than one full lifetime, counts as expired."""
    paths = session_paths(origin)
    cached = _read_json(paths.cache)
    cooldown = _read_json(paths.cooldown)
    if cached is not None and not _cache_record_sound(cached):
        cached = None
    if cooldown is not None and not _cooldown_record_sound(cooldown):
        cooldown = None
    now = _now_ms()
    cache_live = _cache_live(cached, now, cache_ttl_ms)
    cooldown_live = _cooldown_live(cooldown, now, pool_cooldown_ms)
    return SessionState(
        cached=cache_live,
        cache_expires_at=_expires_at(cached) if cache_live else None,
        pool_cooldown_until=_int_field(cooldown, "until") if cooldown_live else None,
    )


def record_pool_full_cooldown(client: BGW320Client, error: BaseException, options: SessionCoordinatorOptions) -> None:
    """Record the pool-full cooldown (and drop the cached session) for a command that runs outside
    `with_router_session`, such as the public sitemap reads: the next command then fails fast instead
    of spending another slot of the small pool. Takes the per-router lock like the coordinator.

    Best-effort, like `_remember_session_pool_full`: a lock timeout or filesystem fault while recording
    is a stderr warning, never a replacement for the pool-full error the caller is about to re-raise."""
    client.clear_session()
    try:
        paths = session_paths(client.session_identity())
        _ensure_private_dir(paths.cache.parent)
        release = _acquire_lock(paths.lock, options.lock_timeout_ms)
        try:
            _remember_session_pool_full(paths, error, options.pool_cooldown_ms)
        finally:
            release()
    except (OSError, BgwError) as failure:
        _warn(f"Could not update local session cooldown; pool-full outcome preserved: {failure}")


def clear_session_state(origin: str, lock_timeout_ms: int = 300000) -> None:
    paths = session_paths(origin)
    _ensure_private_dir(paths.lock.parent)
    release = _acquire_lock(paths.lock, lock_timeout_ms)
    try:
        for path in (paths.cache, paths.cooldown):
            try:
                info = path.lstat()
            except FileNotFoundError:
                info = None
            except OSError as exc:
                raise SessionLockError(f"Cannot remove local router session file {path}: {exc}") from exc
            if info is not None:
                # The same ownership rule as reading a record: another user's file is preserved.
                _require_own_file(path, info)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise SessionLockError(f"Cannot remove local router session file {path}: {exc}") from exc
    finally:
        release()


# --- internals -------------------------------------------------------------------------------


def _within_lifetime(deadline: int, now: int, lifetime_ms: int | None) -> bool:
    """Wall-clock sanity cap: a deadline further ahead than one full lifetime was stamped while the
    clock was wrong (or edited by hand) and would otherwise hold for as long as the clock error."""
    return deadline > now and (lifetime_ms is None or deadline - now <= max(0, lifetime_ms))


def _cache_live(cached: dict[str, Any] | None, now: int, ttl_ms: int | None) -> bool:
    """A cache stamped in the future (cachedAt ahead of now) was written while the clock was ahead.
    A record that carries its stamp keeps its own lifetime otherwise, so a cache written under a
    longer BGW_SESSION_CACHE_TTL_MS (or by the TypeScript CLI) stays usable; only a record without
    a stamp is capped at one configured TTL."""
    if cached is None:
        return False
    cached_at = cached.get("cachedAt")
    if isinstance(cached_at, (int, float)) and not isinstance(cached_at, bool):
        return cached_at <= now < _expires_at(cached)
    return _within_lifetime(_expires_at(cached), now, ttl_ms)


def _cooldown_live(cooldown: dict[str, Any] | None, now: int, cooldown_ms: int | None) -> bool:
    return cooldown is not None and _within_lifetime(_int_field(cooldown, "until"), now, cooldown_ms)


def _apply_cooldown(path: Path, pool_cooldown_ms: int | None = None) -> None:
    cooldown = _read_json(path)
    if cooldown is None:
        return
    if not _cooldown_record_sound(cooldown) or not _cooldown_live(cooldown, _now_ms(), pool_cooldown_ms):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # An expired cooldown blocks nothing; a stuck file is housekeeping, like a stuck cache.
            _warn(f"Could not remove expired local session cooldown {path}; continuing: {exc}")
        return
    raise session_pool_full_error(
        "Router web session pool is full; local cooldown is active.",
        waited_ms=_int_field(cooldown, "waitedMs"),
        retry_count=_int_field(cooldown, "retryCount"),
    )


def _read_cached_session(path: Path) -> dict[str, Any] | None:
    """The coordinator's cache read: our own regular cache file that cannot be read holds nothing
    usable, so it is "nothing cached" (with a warning) and the atomic replace in _persist_session
    overwrites it. A foreign-owned or uninspectable file stays a fault (exit 2), as does any
    unreadable cooldown file (a live cooldown must not be skipped)."""
    try:
        cached = _read_json(path)
        if cached is not None and not _cache_record_sound(cached):
            # Non-finite or mis-typed fields: nothing usable, and a stale copy must not linger.
            _warn(f"Ignoring unusable cached router session {path}; it will be replaced.")
            with suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return cached
    except SessionLockError as error:
        try:
            info = path.lstat()
        except OSError:
            raise error from error.__cause__
        if not _is_regular_file(info) or info.st_uid != os.geteuid():
            raise
        _warn(f"Ignoring unreadable cached router session {path}; it will be replaced: {error.__cause__}")
        return None


def _remember_session_pool_full(paths: SessionPaths, error: BaseException, pool_cooldown_ms: int = 300000) -> None:
    waited_ms, retry_count = pool_full_metadata(error)
    try:
        _validate_private_leaf(paths.cache.parent)
        try:
            # Invalidate stale cookies even when cooldown persistence later fails.
            paths.cache.unlink(missing_ok=True)
        except OSError as failure:
            # A failed deletion must not prevent recording the pool backoff.
            _warn(f"Could not remove cached router session; pool-full outcome preserved: {failure}")
        # Recheck the leaf after deletion before publishing a new record.
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
    except (OSError, BgwError) as failure:
        _warn(f"Could not update local session cooldown; pool-full outcome preserved: {failure}")


def _session_usage(client: BGW320Client) -> tuple[int, int]:
    """Counters that move when the run logged in or got an authenticated answer (0 when absent)."""
    return (
        _counter(getattr(client, "login_attempts", 0)),
        _counter(getattr(client, "authenticated_requests", 0)),
    )


def _counter(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _session_cookies(client: BGW320Client) -> dict[str, str] | None:
    return dict(client.export_session().cookies) if client.has_authenticated_session() else None


def _persist_session(client: BGW320Client, paths: SessionPaths, options: SessionCoordinatorOptions) -> None:
    if not client.has_authenticated_session():
        return
    snapshot = client.export_session()
    now = _now_ms()
    try:
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
    except (OSError, SessionLockError) as error:
        # Router work has completed; an ancillary cache failure cannot undo it.
        _warn(f"Could not persist local router session; command outcome preserved: {error}")


def _forget_cached_session(paths: SessionPaths) -> None:
    try:
        paths.cache.unlink(missing_ok=True)
    except OSError as failure:
        # The command's own authentication error is the outcome; a stuck cache file only warns.
        _warn(f"Could not remove cached router session; command outcome preserved: {failure}")


def _result_pool_full_metadata(value: Any) -> tuple[int, int] | None:
    """Keep observed wait/retry evidence, including flags carried by structured results."""
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return None
    if isinstance(value, (list, tuple)):
        records = [metadata for item in value if (metadata := _result_pool_full_metadata(item)) is not None]
        if not records:
            return None
        # Preserve one observed pair; mirrored results must not double-count it.
        return max(records)
    if isinstance(value, dict):
        if value.get("sessionPoolFull") is not True and value.get("session_pool_full") is not True:
            return None
    else:
        if (getattr(value, "session_pool_full", None) is not True
                and getattr(value, "sessionPoolFull", None) is not True):
            return None
    return pool_full_metadata(value)


def _acquire_lock(path: Path, timeout_ms: int) -> Callable[[], None]:
    try:
        parent_info = path.parent.lstat()
    except OSError as exc:
        raise SessionLockError(f"Cannot prepare session cache directory {path.parent}: {exc}") from exc
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    stale_ms = _lock_stale_ms()
    token = uuid.uuid4().hex
    while True:
        fd = None
        try:
            with _lock_guard(path, deadline):
                permission_error = _remove_stale_lock(path, stale_ms)
                owner = {"pid": os.getpid(), "createdAt": _now_ms(), "token": token, "ownership": _LOCK_OWNERSHIP}
                fd = _publish_lock(path, owner)
                if fd is not None:
                    try:
                        info = os.fstat(fd)
                    except OSError as error:
                        raise _lock_operation_error(path, "inspect the published marker", error) from error
        except BaseException:
            # A published marker already holds our lifetime flock through fd, and only the release
            # closure built below would ever close it. Until that closure exists the descriptor is
            # provisional: close it (dropping the flock) so a later acquisition is not blocked until
            # process exit. The complete marker stays in place for the ordinary stale reclamation;
            # no pathname cleanup here.
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
            raise
        if fd is None:
            _wait_for_lock(deadline, path=path, cause=permission_error)
            continue

        def release(owner_info: os.stat_result = info) -> None:
            nonlocal fd
            if fd is None:
                return
            try:
                # Refuse pathname cleanup through a replaced cache directory.
                current_parent = path.parent.lstat()
                if (
                    not stat.S_ISDIR(current_parent.st_mode)
                    or (current_parent.st_dev, current_parent.st_ino, current_parent.st_uid)
                    != (parent_info.st_dev, parent_info.st_ino, parent_info.st_uid)
                ):
                    return
                # Allow ordinary guard contention to clear, without borrowing the potentially
                # long acquisition timeout or masking the operation's result. Closing our
                # lifetime lock makes any leftover protocol marker reclaimable.
                with _lock_guard(path, time.monotonic() + _LOCK_RELEASE_TIMEOUT_SECONDS):
                    try:
                        current = path.lstat()
                    except FileNotFoundError:
                        return
                    owner = _read_json(path, strict=False)
                    if (
                        (current.st_dev, current.st_ino) == (owner_info.st_dev, owner_info.st_ino)
                        and owner is not None
                        and owner.get("token") == token
                    ):
                        path.unlink(missing_ok=True)
            except (OSError, UnicodeError, SessionLockError):
                pass
            finally:
                owner_fd, fd = fd, None
                with suppress(OSError):
                    os.close(owner_fd)

        return release


def _publish_lock(path: Path, owner: dict[str, Any]) -> int | None:
    """Publish a complete, already locked inode without replacing any existing marker."""
    try:
        fd, temporary = tempfile.mkstemp(prefix=".bgw-lock-", dir=path.parent)
    except OSError as error:
        raise _lock_operation_error(path, "create the ownership record", error) from error
    acquired = False
    failing = False
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError as error:
            raise _lock_operation_error(path, "secure the ownership record", error) from error
        _flock(fd, path)
        content = _dumps(owner).encode()
        while content:
            try:
                written = os.write(fd, content)
                if written <= 0:
                    raise OSError(errno.EIO, "Session lock ownership write made no progress")
            except OSError as error:
                raise _lock_operation_error(path, "write the ownership record", error) from error
            content = content[written:]
        try:
            # POSIX link is exclusive: a crash before this point leaves no reserved marker.
            os.link(temporary, path)
        except FileExistsError:
            return None
        except OSError as error:
            raise _lock_operation_error(
                path, "publish an exclusive hard link", error, hardlink_publication=True,
            ) from error
        acquired = True
        return fd
    except BaseException:
        failing = True
        raise
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError as error:
            with suppress(OSError):
                os.close(fd)
            if failing:
                # Keep the failure already propagating; the leftover record is only reported.
                _warn(f"could not remove the temporary session lock record {temporary}: {error}")
            else:
                raise _lock_operation_error(path, "remove the temporary ownership record", error) from error
        except BaseException:
            os.close(fd)
            raise
        else:
            if not acquired:
                os.close(fd)


@contextmanager
def _lock_guard(path: Path, deadline: float):
    """Serialize marker changes with a mutex whose inode is never removed or replaced."""
    guard = path.with_name(path.name + ".guard")
    try:
        fd = os.open(guard, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        # Same wrapping as every other lock fault: exit 2, operation and path named, cause kept.
        raise _lock_operation_error(path, "open the ownership guard", error) from error
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError as error:
            raise _lock_operation_error(path, "secure the ownership guard", error) from error
        while True:
            try:
                _flock(fd, guard)
                break
            except BlockingIOError:
                _wait_for_lock(deadline)
        yield
    finally:
        os.close(fd)


def _lock_operation_error(
    path: Path, operation: str, error: OSError, *, hardlink_publication: bool = False,
) -> SessionLockError:
    message = f"Cannot {operation} for local router session lock {path}: {error}."
    if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
        message += (
            " This operation is unsupported; set BGW_SESSION_CACHE_DIR to a private directory "
            "on a local filesystem supporting hard links and flock."
        )
    elif hardlink_publication and error.errno == errno.EPERM:
        message += (
            " Hard-link creation may be denied by filesystem capabilities or permissions/policy. "
            "If this filesystem does not support hard links, set BGW_SESSION_CACHE_DIR to a private directory "
            "on a local filesystem supporting hard links and flock."
        )
    elif error.errno == errno.ENOLCK:
        message += " Kernel lock resources are unavailable; check lock-resource availability before retrying."
    return SessionLockError(message)


def _flock(fd: int, path: Path, *, preserve_permissions: bool = False) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, InterruptedError):
        raise
    except OSError as error:
        if preserve_permissions and isinstance(error, PermissionError):
            raise  # Stale ownership inspection retains its conservative permission policy.
        raise _lock_operation_error(path, "acquire kernel flock", error) from error


def _wait_for_lock(
    deadline: float, *, path: Path | None = None, cause: PermissionError | None = None,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        message = "Timed out waiting for local router session lock"
        if path is not None:
            message += f" {path}"
        if cause is not None:
            message += f": ownership inspection failed: {cause}"
        raise SessionLockTimeoutError(message + ".") from cause
    _sleep(min(0.1, remaining))


def _lock_stale_ms() -> float:
    """BGW_SESSION_LOCK_STALE_MS, validated like every other numeric setting (UsageError)."""
    return float(env_number("BGW_SESSION_LOCK_STALE_MS", _DEFAULT_LOCK_STALE_MS, 0))


def _remove_stale_lock(path: Path, stale_ms: float) -> PermissionError | None:
    """Caller holds the guard; a held lifetime lock always wins over age or PID."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except PermissionError as error:
        return error
    except OSError as error:
        raise _lock_operation_error(path, "inspect the existing marker", error) from error
    if not _is_regular_file(info):
        return
    try:
        # O_NONBLOCK: a FIFO swapped in after the lstat above must not block the open.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except PermissionError as error:
        return _unreadable_lock(path, info, stale_ms, error)
    except OSError as error:
        raise _lock_operation_error(path, "open the existing marker", error) from error
    try:
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return
            try:
                _flock(fd, path, preserve_permissions=True)
            except BlockingIOError:
                return
            # Read the record through the descriptor that holds the flock and passed the inode check:
            # a second pathname lookup could land on a replacement marker.
            owner = _read_json_fd(fd)
            complete_protocol = (
                owner is not None and owner.get("ownership") == _LOCK_OWNERSHIP
                and isinstance(owner.get("token"), str) and bool(owner["token"])
                and _is_credible_pid(owner.get("pid"))
                and type(owner.get("createdAt")) is int
            )
            if not complete_protocol:
                # Legacy writers did not hold a lifetime lock. Age alone cannot prove
                # their death, and reused/foreign PIDs are necessarily ambiguous.
                if _now_ms() - info.st_mtime * 1000 <= stale_ms:
                    return
                pid = owner.get("pid") if owner is not None else None
                if _is_credible_pid(pid):
                    if _pid_is_alive(pid):
                        return
                # Without a credible PID, only an owned, single-link reserved file
                # can be treated as a partial record left by an older writer.
                elif info.st_uid != os.geteuid() or info.st_nlink != 1:
                    return
            # Guard participation serializes cooperating writers. Recheck inode and
            # record as well, so an observed replacement is never blindly unlinked.
            try:
                current = path.lstat()
            except FileNotFoundError:
                return
            if ((current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                    or _read_json_fd(fd) != owner):
                return
        except PermissionError as error:
            return _unreadable_lock(path, info, stale_ms, error)
        except (BlockingIOError, InterruptedError):
            raise
        except OSError as error:
            # Marker left in place: an I/O fault is not proof of abandonment.
            raise _lock_operation_error(path, "inspect the existing marker", error) from error
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise _lock_operation_error(path, "remove the abandoned marker", error) from error
    finally:
        os.close(fd)


def _unreadable_lock(
    path: Path, info: os.stat_result, stale_ms: float, error: PermissionError,
) -> PermissionError:
    if _now_ms() - info.st_mtime * 1000 > stale_ms:
        # Waiting cannot make a stale marker readable: a lock fault (exit 2), not contention.
        raise _lock_operation_error(path, "inspect stale ownership (marker preserved)", error) from error
    # A fresh marker may belong to a writer still publishing its ownership record.
    return error


def _is_credible_pid(pid: Any) -> bool:
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _pid_is_alive(pid: Any) -> bool:
    if not _is_credible_pid(pid):
        return True  # Missing or malformed ownership does not prove abandonment.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True  # Permission or other uncertainty cannot establish that the owner died.
    return True


def _read_json(path: Path, *, strict: bool = True) -> dict[str, Any] | None:
    """A missing or malformed file is "nothing cached"; a session/cooldown file that cannot be read
    (permissions, I/O) is a session-coordination fault, reported as SessionLockError (exit 2) instead
    of escaping. Lock markers are read with `strict=False`: their inspection handles the raw
    PermissionError itself (`_unreadable_lock`)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if not strict:
            raise
        if exc.errno in (errno.EACCES, errno.ENOTDIR):
            # lstat needs no access to the file itself: a directory on the way cannot be searched
            # or is not a directory, so name the directory to fix rather than an unreached file.
            raise SessionLockError(
                f"Cannot access local router session directory {path.parent}: {exc.strerror or exc}"
            ) from exc
        raise SessionLockError(f"Cannot read local router session file {path}: {exc}") from exc
    if not _is_regular_file(info):
        return None
    if strict:
        _require_own_file(path, info)
    try:
        # O_NONBLOCK: a FIFO swapped in after the lstat above is refused by the type check below.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if not strict:
            raise
        raise SessionLockError(f"Cannot read local router session file {path}: {exc}") from exc
    try:
        # Validate and read the descriptor that was opened, never the path a second time.
        opened = os.fstat(fd)
        if not _is_regular_file(opened):
            return None
        if strict:
            _require_own_file(path, opened)
        return _read_json_fd(fd)
    except OSError as exc:
        if not strict:
            raise
        raise SessionLockError(f"Cannot read local router session file {path}: {exc}") from exc
    finally:
        os.close(fd)


def _require_own_file(path: Path, info: os.stat_result) -> None:
    """A cache or cooldown record is trusted only when this user owns it: another user's file is
    never imported (it could hand over someone else's cookies or suppress a live cooldown)."""
    if info.st_uid != os.geteuid():
        raise SessionLockError(
            f"Local router session file {path} is owned by another user (uid {info.st_uid}); "
            "remove it or fix its ownership."
        )


def _read_json_fd(fd: int) -> dict[str, Any] | None:
    """Read a whole JSON record from offset 0 of an open descriptor (no pathname lookup); malformed
    content is None like _read_json, I/O errors propagate to the caller's marker policy."""
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write under the session lock; recheck the prepared leaf before persistence."""
    _validate_private_leaf(path.parent)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4()}.tmp")
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        content = memoryview((_dumps(value) + "\n").encode())
        while content:
            # os.write may stop short; only a complete record is ever published.
            written = os.write(fd, content)
            if written <= 0:
                raise OSError(errno.EIO, f"Writing local router session file {path} made no progress")
            content = content[written:]
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


def _validate_private_leaf(directory: Path) -> None:
    """Recheck the private leaf without preparing or repairing its ancestors."""
    try:
        filesystem.reject_filesystem_root(directory, description="Session cache directory")
        filesystem.restore_owner_access(directory, private=True, description="Session cache directory")
    except OSError as exc:
        raise SessionLockError(f"Cannot prepare session cache directory {directory}: {exc}") from exc


def _ensure_private_dir(directory: Path) -> None:
    try:
        # Validate parent aliases before publication; keep the private leaf lexical.
        directory = filesystem.resolve_directory_aliases(directory.parent) / directory.name
        filesystem.reject_filesystem_root(directory, description="Session cache directory")
        filesystem.ensure_directory(directory.parent, description="Session cache parent", allow_aliases=True)
        # The shared check diagnoses unsafe leaf types and ownership below.
        with suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        _validate_private_leaf(directory)
        _reap_stale_leftovers(directory)
    except OSError as exc:
        raise SessionLockError(f"Cannot prepare session cache directory {directory}: {exc}") from exc


def _owned_by_us(info: os.stat_result) -> bool:
    return info.st_uid == os.geteuid()


_RECORD_TEMPORARY = re.compile(
    r"[^/]+\.(?:session|cooldown)\.json\.\d+\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.tmp"
)


# tempfile.mkstemp(prefix=".bgw-lock-") names its file with eight characters from this alphabet.
_LOCK_TEMPORARY = re.compile(r"\.bgw-lock-[a-z0-9_]{8}")


def _reap_stale_leftovers(directory: Path) -> None:
    """Remove this user's crashed-writer leftovers: `.bgw-lock-*` ownership-record temporaries and
    `<key>.(session|cooldown).json.<pid>.<uuid>.tmp` record temporaries (exactly what `_write_json`
    names them) older than the lock stale window. Any other name in a shared cache directory is
    never ours to delete. Only plain files we own are touched
    (never symlinks, directories, foreign files or the lock/cache/cooldown records themselves), and
    a failure is housekeeping: it never fails the command."""
    # Never younger than a minute, whatever the stale-lock setting: another process may be mid-write.
    stale_ms = max(_lock_stale_ms(), 60_000.0)
    now = time.time()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if not (_LOCK_TEMPORARY.fullmatch(name) or _RECORD_TEMPORARY.fullmatch(name)):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            if not _is_regular_file(info) or not _owned_by_us(info) or (now - info.st_mtime) * 1000 <= stale_ms:
                continue
            os.unlink(entry.path)
        except OSError:
            continue


def _dumps(value: Any) -> str:
    """JSON.stringify(): compact separators, key order preserved."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _expires_at(cached: dict[str, Any] | None) -> int:
    return _int_field(cached, "expiresAt")


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float is no usable timestamp
        return False


def _int_field(record: dict[str, Any] | None, key: str) -> int:
    """A finite number as an int; absent, non-numeric and non-finite values are 0 (expired)."""
    if not record:
        return 0
    value = record.get(key)
    return int(value) if _finite_number(value) else 0


def _cache_record_sound(cached: dict[str, Any]) -> bool:
    """False when a stamp is non-finite or mis-typed, or the cookies are not a name -> text mapping."""
    if any(key in cached and not _finite_number(cached[key]) for key in ("cachedAt", "expiresAt")):
        return False
    cookies = cached.get("cookies")
    return cookies is None or (
        isinstance(cookies, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in cookies.items())
    )


def _cooldown_record_sound(cooldown: dict[str, Any]) -> bool:
    return _finite_number(cooldown.get("until")) and all(
        key not in cooldown or _finite_number(cooldown[key]) for key in ("waitedMs", "retryCount")
    )


def _is_regular_file(info: os.stat_result) -> bool:

    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _home_or_tmp() -> str:
    try:
        return str(Path.home())
    except (RuntimeError, KeyError):
        return tempfile.gettempdir()


def _now_ms() -> int:
    return int(time.time() * 1000)
