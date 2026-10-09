"""Shared traversal spine used by sweep/scan/schema/audit/readiness and fixture capture.

One client/session walks the mapped router pages in router-tab order, keeps going through
per-page failures (a page's own 401/403 included; only login failures and login-page bounces
abort), honours a per-page delay and a page
filter, and returns compact metadata per page. Parsed data, form controls and raw HTML are
opt-in so the default result stays small. When the router's web-session pool is full the
remaining pages are marked skipped instead of being fetched.

Collaborator modules (parser, pages, fetch, status, devices) are imported lazily through the
module-level seams below so tests can inject fakes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from .errors import RouterAuthError, RouterSessionPoolFullError, UsageError, is_page_level_auth_error
from .exit_codes import NO_ANSWER
from .types import (
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedPage,
    ParsedSelect,
    ParsedTextarea,
    to_json_dict,
)

UNUSABLE_RESPONSE = "Router returned an unusable response."
TRUNCATED_PAGE = "The page was cut by the parser's bounds; its data is not complete."
SKIPPED_POOL_FULL = "Skipped because router web session pool is full."

_JUNK_TITLE = re.compile(r"^(?:Login|Page not found\.?)$", re.IGNORECASE)


class RouterTabLike(Protocol):
    section: str
    label: str
    page: str


@dataclass(frozen=True)
class SweepProgressEvent:
    index: int
    total: int
    page: str
    phase: Literal["start", "finish"]
    status: Literal["ok", "failed", "skipped"] | None = None


@dataclass
class SweepOptions:
    delay_ms: int = 0
    pages: list[str] | None = None
    include_parsed: bool = False
    include_forms: bool = False
    include_raw: bool = False
    use_fallbacks: bool = True
    include_secrets: bool = False
    on_page_progress: Callable[[SweepProgressEvent], None] | None = None


@dataclass
class SweepControlDetails:
    fields: list[ParsedField]
    selects: list[ParsedSelect]
    textareas: list[ParsedTextarea]
    buttons: list[ParsedButton]
    forms: list[ParsedForm]


@dataclass
class SweepArtifacts:
    html: str | None = None
    parsed: str | None = None


@dataclass
class SweepPage:
    section: str
    label: str
    page: str
    dangerous: bool
    guarded: bool
    ok: bool
    fallback: bool | None = None
    status_code: int | None = None
    error: str | None = None
    # devices: why the IP Allocation fallback could not be read either (set only then).
    fallback_error: str | None = None
    # The parser cut the page at one of its bounds (ParsedPage.truncated): the page is not ok.
    truncated: bool | None = None
    title: str | None = None
    heading: str | None = None
    value_count: int = 0
    value_entry_count: int | None = None
    table_rows: int = 0
    field_count: int = 0
    select_count: int = 0
    textarea_count: int = 0
    button_count: int = 0
    form_count: int = 0
    link_count: int | None = None
    data_count: int = 0
    data_obtainable: bool = False
    useful: bool = False
    not_only_junk: bool = False
    session_pool_full: bool | None = None
    waited_ms: int | None = None
    retry_count: int | None = None
    skipped: bool | None = None
    raw_html: str | None = None
    parsed: ParsedPage | None = None
    controls: SweepControlDetails | None = None
    fallback_sections: list[Any] | None = None
    devices: list[Any] | None = None
    artifacts: SweepArtifacts | None = None


# ---------------------------------------------------------------------------
# Lazy collaborator seams (monkeypatched in tests)


def _router_tabs() -> list[Any]:
    from .pages import router_tabs

    return list(router_tabs)


def _resolve_page(name: str) -> str:
    from .pages import resolve_page

    return resolve_page(name)


def _parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    from .parser import parse_page

    return parse_page(page, html, include_secrets=include_secrets)


def _parsed_data_count(parsed: ParsedPage) -> int:
    from .fetch import parsed_data_count

    return parsed_data_count(parsed)


def _fetch_parsed_page(client: Any, page: str, include_secrets: bool = False) -> Any:
    from .fetch import fetch_parsed_page

    return fetch_parsed_page(client, page, include_secrets=include_secrets)


def _fetch_device_list(client: Any) -> Any:
    from .devices import fetch_device_list

    return fetch_device_list(client)


def _fetch_device_status(client: Any, include_secrets: bool = False) -> Any:
    from .status import fetch_device_status

    return fetch_device_status(client, include_secrets=include_secrets)


def _fetch_home_network_status(client: Any, include_secrets: bool = False) -> Any:
    from .status import fetch_home_network_status

    return fetch_home_network_status(client, include_secrets=include_secrets)


def _fetch_security_options(client: Any, include_secrets: bool = False) -> Any:
    from .status import fetch_security_options

    return fetch_security_options(client, include_secrets=include_secrets)


def _sleep(ms: int) -> None:
    time.sleep(ms / 1000)


# ---------------------------------------------------------------------------
# Public API


def sweep_router(client: Any, options: SweepOptions | None = None) -> list[SweepPage]:
    options = options or SweepOptions()
    tabs = sweep_tabs(options.pages)
    total = len(tabs)
    pages: list[SweepPage] = []

    for index, tab in enumerate(tabs, start=1):
        _progress(options, SweepProgressEvent(index, total, tab.page, "start"))
        page = _safe_sweep_page(client, tab, options)
        pages.append(page)
        _progress(options, SweepProgressEvent(index, total, tab.page, "finish", _page_status(page)))
        if page.session_pool_full:
            for skipped_index, skipped_tab in enumerate(tabs[index:], start=index + 1):
                skipped = _skipped_page(skipped_tab)
                pages.append(skipped)
                _progress(options, SweepProgressEvent(skipped_index, total, skipped.page, "finish", "skipped"))
            break
        if options.delay_ms > 0 and index < total:
            _sleep(options.delay_ms)

    return pages


def sweep_exit_code(pages: list[SweepPage]) -> int:
    """Exit status for a finished sweep: no answer (2) when pages were swept and none succeeded.

    A sweep in which at least one page loaded is still an answer (0); the per-page errors stay in
    the structured result either way.
    """
    if pages and not any(page.ok for page in pages):
        return NO_ANSWER
    return 0


def preflight_sweep_output(out_dir: str | Path, pages: list[str] | None = None) -> None:
    """Refuse an unusable ``--out`` tree (see ``_preflight_output_tree``) before the sweep starts
    walking the router, per-page files included: the page list is known up front (``pages``, or
    every router page when not given), so a refusal leaves no partial artifacts."""
    out = Path(out_dir)
    html_dir = out / "router-html"
    parsed_dir = out / "parsed"
    files = [out / "sweep.json"]
    for tab in sweep_tabs(pages):
        files += [html_dir / f"{tab.page}.html", parsed_dir / f"{tab.page}.json"]
    _preflight_output_tree(out, [html_dir, parsed_dir], files)


def write_sweep_artifacts(pages: list[SweepPage], out_dir: str | Path) -> list[SweepPage]:
    """Write router-html/<page>.html and parsed/<page>.json per page plus a compact sweep.json.

    Artifacts carry raw router HTML, so every file is written UTF-8 with mode 0600 and the
    artifact directories are 0700, whatever the umask or the mode of a file being overwritten. An
    output directory that already exists keeps its mode; one created here is 0700. Nothing is
    written through a symlink or into another user's entry: an artifact directory or file that is
    a symlink, not of the expected type, or owned by another user is refused (PermissionError,
    entry preserved), and each file is published by an atomic rename of a freshly created private
    file, so an existing artifact is replaced rather than truncated in place.
    """
    out = Path(out_dir)
    html_dir = out / "router-html"
    parsed_dir = out / "parsed"
    targets = [out / "sweep.json"]
    for page in pages:
        targets += [html_dir / f"{page.page}.html", parsed_dir / f"{page.page}.json"]
    _preflight_output_tree(out, [html_dir, parsed_dir], targets)
    _make_private_dir(out, chmod_existing=False)
    _make_private_dir(html_dir, chmod_existing=True)
    _make_private_dir(parsed_dir, chmod_existing=True)

    with_artifacts: list[SweepPage] = []
    for page in pages:
        artifacts = SweepArtifacts()
        if page.raw_html is not None:
            html_path = html_dir / f"{page.page}.html"
            _write_private_text(html_path, page.raw_html.rstrip() + "\n")
            artifacts.html = str(html_path)
        if page.parsed is not None:
            parsed_path = parsed_dir / f"{page.page}.json"
            _write_private_text(parsed_path, json.dumps(to_json_dict(page.parsed), indent=2) + "\n")
            artifacts.parsed = str(parsed_path)
        has_artifacts = artifacts.html is not None or artifacts.parsed is not None
        with_artifacts.append(strip_large_payloads(replace(page, artifacts=artifacts if has_artifacts else None)))

    _write_private_text(out / "sweep.json", json.dumps(to_json_dict(with_artifacts), indent=2) + "\n")
    return with_artifacts


def _preflight_output_tree(root: Path, directories: list[Path], files: list[Path]) -> None:
    """Refuse an unusable output tree before anything is created or written: the output root and
    every artifact directory and file that already exists must be a real entry of the expected
    type owned by the effective user (a symlink or another user's entry is refused,
    PermissionError, and the root being a file is NotADirectoryError). Nothing is left behind on a
    refusal. Symlinks in the path *above* the root (a ``/tmp`` that is itself a link) are fine."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return  # a root that does not exist yet has no children to refuse
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"Output directory is a symlink; refusing to write through it; preserved: {root}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"Output path is not a directory; preserved: {root}")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"Output directory is owned by another user; preserved: {root}")
    for directory in directories:
        _require_owned_entry(directory, directory=True)
    for file in files:
        # A file below a directory that does not exist yet cannot already exist.
        if file.parent.exists() or file.parent.is_symlink():
            _require_owned_entry(file, directory=False)


def _make_private_dir(path: Path, *, chmod_existing: bool) -> None:
    """Create ``path`` (0700) or accept it as it is.

    ``chmod_existing`` marks an artifact directory this tool owns (``router-html/``, ``parsed/``):
    an existing one must be a real directory owned by the effective user, never a symlink, and is
    tightened to 0700. The output root itself (``chmod_existing=False``) keeps its mode; callers
    run ``_preflight_output_tree`` first, which refuses a symlinked or foreign root."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not chmod_existing:
            if not path.is_dir():
                raise NotADirectoryError(f"Output path is not a directory; preserved: {path}") from None
            return
        _require_owned_entry(path, directory=True)
    # mkdir's mode is filtered by the umask; chmod sets it exactly. The entry was checked not to
    # be a symlink, so the change cannot land on another file.
    path.chmod(0o700)


def _require_owned_entry(path: Path, *, directory: bool) -> bool:
    """True when ``path`` exists as an entry this tool may replace or write into; False when it is
    missing. A symlink, an entry of the wrong type or one owned by another user is refused."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    kind = "directory" if directory else "file"
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"Artifact {kind} is a symlink; refusing to write through it; preserved: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise PermissionError(f"Artifact path is not a regular {kind}; preserved: {path}")
    if info.st_uid != os.geteuid():
        raise PermissionError(f"Artifact {kind} is owned by another user; preserved: {path}")
    return True


def _write_private_text(path: Path, text: str) -> None:
    """Publish ``text`` at ``path`` (UTF-8, 0600) without following or truncating what is there:
    the content goes to a new private file created with O_EXCL|O_NOFOLLOW beside ``path`` and is
    renamed over it, after the existing entry (if any) was checked to be our own regular file."""
    _require_owned_entry(path.parent, directory=True)
    _require_owned_entry(path, directory=False)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        try:
            # The creation mode is filtered by the umask.
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def strip_large_payloads(page: SweepPage) -> SweepPage:
    return replace(page, raw_html=None, parsed=None, controls=None)


def sweep_tabs(pages: list[str] | None) -> list[Any]:
    """Deduplicated router tabs, optionally filtered (in router-tab order) by page names/aliases."""
    unique_tabs = _dedupe_tabs(_router_tabs())
    if not pages:
        return unique_tabs

    selected = {resolved for resolved in (_resolve_page(page.strip()) for page in pages) if resolved}
    tabs = [tab for tab in unique_tabs if tab.page in selected]
    known = {tab.page for tab in tabs}
    unknown = [page for page in selected if page not in known]
    if unknown:
        raise UsageError(f"Unknown sweep page(s): {', '.join(sorted(unknown))}")
    return tabs


def is_junk_title(title: str) -> bool:
    return bool(_JUNK_TITLE.match(title))


def is_junk_only(parsed: ParsedPage) -> bool:
    return is_junk_title(parsed.title or parsed.heading) or _parsed_data_count(parsed) == 0


# ---------------------------------------------------------------------------
# Internals


def _progress(options: SweepOptions, event: SweepProgressEvent) -> None:
    if options.on_page_progress is not None:
        options.on_page_progress(event)


def _dedupe_tabs(tabs: list[Any]) -> list[Any]:
    seen: set[str] = set()
    unique: list[Any] = []
    for tab in tabs:
        if tab.page in seen:
            continue
        seen.add(tab.page)
        unique.append(tab)
    return unique


def _safe_sweep_page(client: Any, tab: Any, options: SweepOptions) -> SweepPage:
    try:
        return _sweep_page(client, tab, options)
    except RouterSessionPoolFullError as error:
        return _failure_page(tab, error)
    except RouterAuthError as error:
        if is_page_level_auth_error(error):
            # One page answering 401/403 is that page's failure; the sweep continues.
            return _failure_page(tab, error)
        raise
    except Exception as error:  # noqa: BLE001 - per-page failures must not stop the sweep
        return _failure_page(tab, error)


def _sweep_page(client: Any, tab: Any, options: SweepOptions) -> SweepPage:
    use_fallbacks = options.use_fallbacks and not options.include_raw
    if use_fallbacks:
        fallback = _fallback_page(client, tab, options)
        if fallback is not None:
            return fallback

    response = client.get_cgi_page(tab.page, auth=False) if tab.page == "sitemap" else client.get_cgi_page(tab.page)
    parsed = _parse_page(tab.page, response.body, include_secrets=options.include_secrets)
    return _page_from_parsed(
        tab,
        parsed,
        options,
        status_code=response.status_code,
        ok=_status_ok(response.status_code) and not is_junk_only(parsed),
        raw_html=response.body if options.include_raw else None,
    )


def _fallback_page(client: Any, tab: Any, options: SweepOptions) -> SweepPage | None:
    if tab.page == "devices":
        result = _fetch_device_list(client)
        devices = list(result.devices)
        # ok means the list was read: devices.ha answered (even with zero online devices) or the
        # IP Allocation fallback produced devices. A fallback with nothing to show did not answer.
        answered = not result.fallback or len(devices) > 0
        return _complete_page(
            _base_page(tab, ok=answered),
            fallback=result.fallback,
            title="Device List fallback" if result.fallback else "Device List",
            table_rows=len(devices),
            data_count=len(devices),
            devices=devices if options.include_parsed else None,
            error=result.error or None,
            fallback_error=getattr(result, "fallback_error", None) or None,
        )

    status_views = {
        "home": (_fetch_device_status, "Device Status fallback"),
        "lanstatistics": (_fetch_home_network_status, "Home Network Status fallback"),
        "securityoptions": (_fetch_security_options, "Security Options fallback"),
    }
    if tab.page in status_views:
        fetch, fallback_title = status_views[tab.page]
        status = fetch(client, include_secrets=options.include_secrets)
        if status.fallback:
            return _fallback_status_page(
                tab, fallback_title, status.error, list(status.sections), options.include_parsed
            )
        if status.parsed is not None:
            return _page_from_parsed(
                tab,
                status.parsed,
                options,
                status_code=status.status_code,
                ok=status.status_code is None or _status_ok(status.status_code),
            )

    result = _fetch_parsed_page(client, tab.page, include_secrets=options.include_secrets)
    if result.ok and result.parsed is not None:
        return _page_from_parsed(tab, result.parsed, options, status_code=result.status_code, ok=True)
    if result.parsed is not None:
        return _page_from_parsed(
            tab,
            result.parsed,
            options,
            status_code=result.status_code,
            ok=False,
            error=result.error or UNUSABLE_RESPONSE,
        )
    return _complete_page(_base_page(tab, ok=False), error=result.error or UNUSABLE_RESPONSE)


def _fallback_status_page(
    tab: Any, title: str, error: str | None, sections: list[Any], include_sections: bool
) -> SweepPage:
    value_count = sum(len(section.values) for section in sections)
    table_rows = sum(len(section.tables) for section in sections)
    return _complete_page(
        _base_page(tab, ok=any(section.ok for section in sections)),
        fallback=True,
        title=title,
        error=error or None,
        value_count=value_count,
        table_rows=table_rows,
        data_count=value_count + table_rows,
        fallback_sections=sections if include_sections else None,
    )


def _page_from_parsed(
    tab: Any,
    parsed: ParsedPage,
    options: SweepOptions,
    *,
    ok: bool,
    status_code: int | None = None,
    error: str | None = None,
    raw_html: str | None = None,
) -> SweepPage:
    truncated = bool(parsed.truncated)
    if truncated:
        ok, error = False, error or TRUNCATED_PAGE
    return _complete_page(
        _base_page(tab, ok=ok),
        truncated=True if truncated else None,
        title=parsed.title,
        heading=parsed.heading,
        value_count=len(parsed.values),
        value_entry_count=len(parsed.value_entries or []),
        table_rows=len(parsed.tables),
        field_count=len(parsed.fields),
        select_count=len(parsed.selects),
        textarea_count=len(parsed.textareas),
        button_count=len(parsed.buttons),
        form_count=len(parsed.forms),
        link_count=len(parsed.links or []),
        data_count=_parsed_data_count(parsed),
        status_code=status_code,
        error=error or None,
        parsed=parsed if options.include_parsed else None,
        controls=_controls_from_parsed(parsed) if options.include_forms else None,
        raw_html=raw_html,
    )


def _controls_from_parsed(parsed: ParsedPage) -> SweepControlDetails:
    return SweepControlDetails(
        fields=parsed.fields,
        selects=parsed.selects,
        textareas=parsed.textareas,
        buttons=parsed.buttons,
        forms=parsed.forms,
    )


def _base_page(tab: Any, *, ok: bool) -> SweepPage:
    dangerous = bool(getattr(tab, "dangerous", False))
    return SweepPage(section=tab.section, label=tab.label, page=tab.page, dangerous=dangerous, guarded=dangerous, ok=ok)


def _complete_page(page: SweepPage, **overrides: Any) -> SweepPage:
    page = replace(page, **overrides)
    data_obtainable = page.data_count > 0
    title = page.title if page.title is not None else (page.heading or "")
    return replace(
        page,
        data_obtainable=data_obtainable,
        useful=page.ok and data_obtainable,
        not_only_junk=page.ok and data_obtainable and not is_junk_title(title),
    )


def _failure_page(tab: Any, error: BaseException) -> SweepPage:
    extra: dict[str, Any] = {}
    if isinstance(error, RouterSessionPoolFullError):
        extra = {
            "session_pool_full": True,
            "waited_ms": int(getattr(error, "waited_ms", 0) or 0),
            "retry_count": int(getattr(error, "retry_count", 0) or 0),
        }
    return _complete_page(_base_page(tab, ok=False), error=str(error) or error.__class__.__name__, **extra)


def _skipped_page(tab: Any) -> SweepPage:
    return _complete_page(_base_page(tab, ok=False), skipped=True, session_pool_full=True, error=SKIPPED_POOL_FULL)


def _page_status(page: SweepPage) -> Literal["ok", "failed", "skipped"]:
    if page.skipped:
        return "skipped"
    return "ok" if page.ok else "failed"


def _status_ok(status_code: int) -> bool:
    return 200 <= status_code < 400
