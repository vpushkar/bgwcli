"""Directory publication and permission repair for local private state."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path


def reject_filesystem_root(directory: Path, *, description: str = "Directory") -> None:
    """Refuse root aliases while leaving inaccessible or cyclic entries for caller validation."""
    physical = Path(os.path.realpath(directory))
    if physical.parent == physical:
        raise PermissionError(f"{description} cannot be the filesystem root; preserved: {directory}")


def restore_owner_access(
    directory: Path, *, private: bool = False, description: str = "Directory",
) -> None:
    """Restore owner access only on validated, effective-user-owned directories."""
    created = directory.lstat()
    if stat.S_ISLNK(created.st_mode):
        raise PermissionError(f"{description} is a symlink; preserved: {directory}")
    if not stat.S_ISDIR(created.st_mode) or created.st_uid != os.geteuid():
        raise PermissionError(f"{description} ownership/type changed; preserved: {directory}")
    mode = 0o700 if private else stat.S_IMODE(created.st_mode) | 0o700
    if stat.S_IMODE(created.st_mode) == mode:
        return
    try:
        directory.chmod(mode, follow_symlinks=False)
    except NotImplementedError:
        # Older Linux libc rejects no-follow chmod even for ordinary directories.
        # O_PATH opens mode-000 crash remnants without first changing their permissions.
        path_only = getattr(os, "O_PATH", 0)
        try:
            descriptor = os.open(directory, (path_only or os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise PermissionError(
                    f"{description} ownership/type changed; preserved: {directory}",
                ) from exc
            raise
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode), opened.st_uid) != (
                created.st_dev, created.st_ino, stat.S_IFMT(created.st_mode), created.st_uid,
            ):
                raise PermissionError(f"{description} changed; preserved: {directory}")
            mode = 0o700 if private else stat.S_IMODE(opened.st_mode) | 0o700
            if path_only:
                # This proc link refers to the validated inode, even if its original
                # pathname changes. Missing procfs fails closed without a path fallback.
                os.chmod(f"/proc/self/fd/{descriptor}", mode)
            else:
                os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
    else:
        # Some platforms can chmod a symlink itself successfully. Refuse that
        # replacement before a caller proceeds to create files through it.
        current = directory.lstat()
        if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode), current.st_uid) != (
            created.st_dev, created.st_ino, stat.S_IFMT(created.st_mode), created.st_uid,
        ):
            raise PermissionError(f"{description} changed; preserved: {directory}")


def _lstat_or_none(path: Path) -> os.stat_result | None:
    """No-follow entry metadata; only a missing entry is None, other failures propagate."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def reject_directory_symlink(directory: Path, *, description: str = "Directory") -> None:
    info = _lstat_or_none(directory)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"{description} is a symlink; preserved: {directory}")


def sync_directory(directory: Path) -> None:
    """Commit directory entries on the supported macOS/Linux filesystems; failures propagate."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise OSError(
                exc.errno,
                f"Cannot synchronize directory: {exc}. Directory fsync is required for durable directory "
                "updates; choose BGW_SESSION_CACHE_DIR (or the recovery state directory) on a local "
                "filesystem that supports it, or remove the pending .bgw-publication-*.pending marker "
                "beside the directory after confirming it is fully published",
                str(directory),
            ) from exc
        raise
    finally:
        os.close(descriptor)


def _publication_marker(directory: Path) -> Path:
    """A private sibling records a base entry whose parent still needs synchronization."""
    name_hash = hashlib.sha256(os.fsencode(directory.name)).hexdigest()[:24]
    return directory.with_name(f".bgw-publication-{name_hash}.pending")


def _is_directory(directory: Path, *, description: str = "Directory") -> bool:
    """Preserve access failures across Python versions; only missing paths are false.

    Callers pass canonical entries, so a symlink here is never a permitted alias; it is
    diagnosed as such before following it would misreport its target's type.
    """
    info = _lstat_or_none(directory)
    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"{description} is a symlink; preserved: {directory}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{description} is not a directory; preserved: {directory}")
    return True


def resolve_directory_aliases(directory: Path, *, description: str = "Directory") -> Path:
    """Resolve existing directory aliases without erasing missing traversal components."""
    absolute = directory.absolute()
    resolved = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        candidate = resolved / component
        try:
            info = os.lstat(candidate)
        except (FileNotFoundError, PermissionError):
            # Missing ordinary parents are publishable. Inaccessible suffixes stay
            # lexical until ensure_directory can repair a validated pending ancestor.
            resolved = candidate
            continue
        if stat.S_ISLNK(info.st_mode):
            try:
                candidate = candidate.resolve(strict=True)
            except RuntimeError as exc:
                # Python 3.10-3.12 report symlink loops outside the OSError hierarchy.
                raise OSError(errno.ELOOP, f"{description} alias is cyclic", str(candidate)) from exc
            if not stat.S_ISDIR(candidate.stat().st_mode):
                raise NotADirectoryError(f"{description} alias is not a directory; preserved: {candidate}")
        elif not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(f"{description} component is not a directory; preserved: {candidate}")
        elif component == "..":
            candidate = resolved.parent
        resolved = candidate
    return resolved


def _canonical_entry(directory: Path, *, allow_aliases: bool, description: str) -> Path:
    """Canonicalise a base entry; without alias permission only its parents may be aliases."""
    if allow_aliases:
        return resolve_directory_aliases(directory, description=description)
    return resolve_directory_aliases(directory.parent, description=description) / directory.name


def ensure_directory(
    directory: Path, *, boundary: Path | None = None, private: bool = False,
    description: str = "Directory", allow_aliases: bool = False,
) -> None:
    """Publish owned entries and new base directories; trust unmarked existing ancestors.

    A marker precedes each new base mkdir and survives failed synchronization, so another
    invocation retries publication even if a concurrent child prevents removing the directory.
    Owned entries are always synchronized; unrelated established ancestors are not republished.
    Cache parents opt into aliases; owned leaves and direct base entries keep no-follow validation.
    """
    # Base ancestors may be aliases. Owned application components remain lexical
    # until recursion has validated and repaired each parent before its child.
    if boundary is None:
        directory = _canonical_entry(directory, allow_aliases=allow_aliases, description=description)
    else:
        directory = directory.absolute()
    if boundary is not None:
        boundary = _canonical_entry(boundary, allow_aliases=False, description=description)
        if directory != boundary and boundary not in directory.parents:
            raise ValueError("directory boundary must be a physical ancestor")
    if directory == boundary:
        if private:
            restore_owner_access(directory, private=True, description=description)
        return
    if boundary is None:
        try:
            existing = _is_directory(directory, description=description)
        except PermissionError:
            # A crash may leave an earlier newly created ancestor without owner
            # access. Its validated sibling marker is the authority to repair it.
            ensure_directory(directory.parent, description=description, allow_aliases=True)
            directory = _canonical_entry(directory, allow_aliases=allow_aliases, description=description)
            existing = _is_directory(directory, description=description)  # Unmarked/inaccessible parents still fail.
        if existing:
            reject_directory_symlink(directory, description=description)
            if directory.parent == directory:
                return
            # Each parent is published before a child is created. Only this entry can
            # still be pending; established ancestors need no probes or synchronization.
            marker = _publication_marker(directory)
            pending = _read_publication_marker(marker, directory)
            if pending is not None:
                restore_owner_access(directory, description=description)
                sync_directory(directory.parent)
                _remove_publication_marker(marker, directory, pending)
            return
    if directory.parent == directory:
        raise NotADirectoryError(f"cannot create directory root: {directory}")
    ensure_directory(
        directory.parent, boundary=boundary, description=description, allow_aliases=boundary is None,
    )
    if boundary is None:
        # Repair may reveal aliases in a previously inaccessible parent. Revalidate
        # before publishing anything below it; never erase them with non-strict realpath.
        previous_directory = directory
        directory = _canonical_entry(directory, allow_aliases=allow_aliases, description=description)
        if directory != previous_directory:
            # Traversable '..' may now identify an established ancestor, including
            # root. Reclassify it before granting any new publication authority.
            ensure_directory(directory, private=private, description=description, allow_aliases=allow_aliases)
            return
        if directory.name == "..":
            ensure_directory(resolve_directory_aliases(directory, description=description), description=description)
            return
        reject_directory_symlink(directory, description=description)
    existing = boundary is not None and _lstat_or_none(directory) is not None
    marker = _publication_marker(directory) if boundary is None else None
    if marker is not None:
        # Create before mkdir: even failure to create the marker cannot strand an unmarked entry.
        pending = _create_publication_marker(marker, directory)
    created = False
    if not existing:
        try:
            directory.mkdir(mode=0o700 if boundary is not None else 0o777)
        except FileExistsError:
            if boundary is None:
                # A raced non-directory entry leaves nothing to publish; the reservation stays.
                # A stale marker is safe (it only repeats the required parent sync, see the end
                # of this function), whereas removing it could delete a reservation that a
                # concurrent publisher adopted from _create_publication_marker, and any cleanup
                # probe here would mask the original diagnosis.
                existing = _is_directory(directory, description=description)
                if not existing:
                    raise
        else:
            created = True
    if boundary is not None or created:
        # Owned entries share one continuation, whether found, created, or raced.
        # A race-created base retains its creator's permissions.
        restore_owner_access(directory, private=private, description=description)
    sync_directory(directory.parent)
    if marker is not None:
        # A stale marker after a crash is safe: it only repeats the required parent sync.
        _remove_publication_marker(marker, directory, pending)


def _marker_content(directory: Path) -> bytes:
    return json.dumps({"version": 1, "directory": str(directory.resolve())}, sort_keys=True).encode()


def _matches_marker(content: bytes, directory: Path) -> bool:
    # Legacy private empty reservations predate the versioned directory record.
    if content == b"":
        return True
    try:
        record = json.loads(content)
        if not isinstance(record, dict) or set(record) != {"version", "directory"}:
            return False
        if type(record["version"]) is not int or record["version"] != 1:
            return False
        if not isinstance(record["directory"], str):
            return False
        recorded = Path(record["directory"])
        # A legacy alias is evidence only; synchronization and removal always use
        # the caller's paths. Missing ancestors must not vanish through lexical '..'.
        if not recorded.is_absolute():
            return False
        try:
            return recorded.resolve(strict=True) == directory.resolve(strict=True)
        except FileNotFoundError:
            # A marker precedes mkdir, but both parent directories must already exist.
            return (
                recorded.name == directory.name
                and recorded.parent.resolve(strict=True) == directory.parent.resolve(strict=True)
            )
    except (ValueError, OSError, RuntimeError):
        return False


def _read_publication_marker(marker: Path, directory: Path) -> os.stat_result | None:
    """Only our private, single-link regular records authorize publication or deletion."""
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
    ):
        raise PermissionError(f"Unsafe directory publication marker; preserved: {marker}")
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_nlink) != (
            info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        ):
            raise PermissionError(f"Directory publication marker changed; preserved: {marker}")
        # Legacy aliases may be longer than the canonical spelling; cap untrusted input.
        content = os.read(descriptor, 65537)
        if len(content) > 65536 or not _matches_marker(content, directory):
            raise PermissionError(f"Unknown directory publication marker content; preserved: {marker}")
        try:
            current = marker.lstat()
        except FileNotFoundError:
            return None
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise PermissionError(f"Directory publication marker replaced; preserved: {marker}")
        return info
    finally:
        os.close(descriptor)


def _create_publication_marker(marker: Path, directory: Path) -> os.stat_result:
    """Return the validated reservation, created here or adopted from an earlier publisher."""
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pending = _read_publication_marker(marker, directory)
        if pending is None:
            # A concurrent publisher finished between exclusive open and inspection.
            return _create_publication_marker(marker, directory)
        return pending
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(_marker_content(directory))
        stream.flush()
        os.fsync(stream.fileno())
        return os.fstat(stream.fileno())


def _remove_publication_marker(marker: Path, directory: Path, pending: os.stat_result) -> None:
    current = _read_publication_marker(marker, directory)
    if current is None:
        return  # A concurrent publisher already completed the same parent sync.
    if (current.st_dev, current.st_ino) != (pending.st_dev, pending.st_ino):
        raise PermissionError(f"Directory publication marker replaced; preserved: {marker}")
    marker.unlink(missing_ok=True)
