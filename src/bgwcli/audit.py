"""Health / usefulness summaries over sweep results.

`build_audit` powers `bgwcli audit` / `readiness`. `expected_fixture`, `write_fixture_set` and
`capture_fixture_pack` produce the gitignored router fixture pack (router-html/, parsed/,
expected/ under tests/fixtures) with the same evidence fields as the TS capture script.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from . import sweep
from .errors import BgwError
from .fixture_sanitize import (
    FixtureBoundsError,
    contains_sensitive_fixture_value,
    fixture_secret_values,
    sanitize_router_fixture,
    sensitive_control_residue,
)
from .sweep import SweepPage, is_junk_title
from .terminal import sanitize_terminal_text
from .types import ParsedPage, to_json_dict

FIXTURE_HTML_DIR = "router-html"
FIXTURE_PARSED_DIR = "parsed"
FIXTURE_EXPECTED_DIR = "expected"


class FixtureSafetyError(BgwError):
    """Sanitized fixture output still contains recognizable sensitive data."""


@dataclass
class AuditResult:
    total_pages: int
    ok_pages: int
    failed_pages: int
    fallback_pages: int
    useful_pages: int
    empty_pages: int
    dangerous_pages: int
    pages: list[SweepPage]


def build_audit(scans: list[SweepPage]) -> AuditResult:
    pages = [replace(scan, useful=scan.ok and scan.data_count > 0) for scan in scans]
    return AuditResult(
        total_pages=len(pages),
        ok_pages=sum(1 for page in pages if page.ok),
        failed_pages=sum(1 for page in pages if not page.ok),
        fallback_pages=sum(1 for page in pages if page.fallback),
        useful_pages=sum(1 for page in pages if page.useful),
        empty_pages=sum(1 for page in pages if page.ok and not page.useful),
        dangerous_pages=sum(1 for page in pages if page.dangerous),
        pages=pages,
    )


@dataclass(frozen=True)
class FixtureCounts:
    values: int
    value_entries: int
    tables: int
    fields: int
    selects: int
    textareas: int
    buttons: int
    forms: int
    links: int


@dataclass(frozen=True)
class ExpectedFixture:
    page: str
    title: str
    page_loads: bool
    data_obtainable: bool
    useful_fields_exist: bool
    useful_tables_exist: bool
    buttons_discovered: bool
    forms_discovered: bool
    secrets_redacted: bool
    not_only_junk: bool
    counts: FixtureCounts
    value_keys: list[str]
    table_columns: list[str]
    field_names: list[str]
    select_names: list[str]
    textarea_names: list[str]
    button_names: list[str]
    form_actions: list[str]
    link_targets: list[str]
    value_entry_labels: list[str]
    error: str | None = None
    # The parser cut the page at one of its bounds (ParsedPage.truncated); absent otherwise.
    truncated: bool | None = None


def expected_fixture(
    page: str, parsed: ParsedPage, *, page_loads: bool, data_count: int | None = None
) -> ExpectedFixture:
    if data_count is None:
        data_count = sweep._parsed_data_count(parsed)
    table_columns = sorted({column for row in parsed.tables for column in row})
    serialized = json.dumps(to_json_dict(parsed), separators=(",", ":"))
    value_entries = parsed.value_entries or []
    links = parsed.links or []
    return ExpectedFixture(
        page=page,
        title=parsed.title,
        page_loads=page_loads,
        data_obtainable=data_count > 0,
        useful_fields_exist=any(field.name not in ("nonce", "hashpassword") for field in parsed.fields),
        useful_tables_exist=len(parsed.tables) > 0 and len(table_columns) > 0,
        buttons_discovered=len(parsed.buttons) > 0,
        forms_discovered=len(parsed.forms) > 0,
        secrets_redacted=not contains_sensitive_fixture_value(serialized, html=False),
        not_only_junk=data_count > 0 and not is_junk_title(parsed.title or parsed.heading),
        counts=FixtureCounts(
            values=len(parsed.values),
            value_entries=len(value_entries),
            tables=len(parsed.tables),
            fields=len(parsed.fields),
            selects=len(parsed.selects),
            textareas=len(parsed.textareas),
            buttons=len(parsed.buttons),
            forms=len(parsed.forms),
            links=len(links),
        ),
        value_keys=sorted(parsed.values),
        table_columns=table_columns,
        field_names=sorted(field.name for field in parsed.fields),
        select_names=sorted(select.name for select in parsed.selects),
        textarea_names=sorted(textarea.name for textarea in parsed.textareas),
        button_names=sorted(button.name for button in parsed.buttons),
        form_actions=sorted({form.action for form in parsed.forms}),
        link_targets=sorted({link.href for link in links}),
        value_entry_labels=sorted({entry.label for entry in value_entries}),
        truncated=True if parsed.truncated else None,
    )


def write_fixture_set(
    fixture_root: str | Path,
    page: str,
    html: str,
    parsed: ParsedPage,
    expected: ExpectedFixture,
    *,
    error: str | None = None,
) -> list[Path]:
    """Write router-html/<page>.html, parsed/<page>.json, expected/<page>.json.

    Same file discipline as sweep artifacts: UTF-8, files 0600 and the three fixture directories
    0700 whatever the umask or an existing file's mode; an existing fixture root keeps its mode.
    """
    root = Path(fixture_root)
    directories = [root / FIXTURE_HTML_DIR, root / FIXTURE_PARSED_DIR, root / FIXTURE_EXPECTED_DIR]
    paths = [
        directories[0] / f"{page}.html",
        directories[1] / f"{page}.json",
        directories[2] / f"{page}.json",
    ]
    sweep._preflight_output_tree(root, directories, paths)
    sweep._make_private_dir(root, chmod_existing=False)
    for directory in directories:
        sweep._make_private_dir(directory, chmod_existing=True)
    expected_json = to_json_dict(expected)
    if error:
        expected_json["error"] = error
    contents = [
        html.rstrip() + "\n",
        json.dumps(to_json_dict(parsed), indent=2) + "\n",
        json.dumps(expected_json, indent=2) + "\n",
    ]
    for path, content in zip(paths, contents, strict=True):
        sweep._write_private_text(path, content)
    return paths


def preflight_fixture_root(fixture_root: str | Path, pages: list[str] | None = None) -> None:
    """Refuse an unusable fixture root, per-page files included, before the capture starts walking the
    router (``pages``, or every router page when not given)."""
    root = Path(fixture_root)
    directories = [root / FIXTURE_HTML_DIR, root / FIXTURE_PARSED_DIR, root / FIXTURE_EXPECTED_DIR]
    files = [
        directory / f"{tab.page}.{extension}"
        for tab in sweep.sweep_tabs(pages)
        for directory, extension in zip(directories, ("html", "json", "json"), strict=True)
    ]
    sweep._preflight_output_tree(root, directories, files)


def _line(text: str) -> str:
    return sanitize_terminal_text(text, single_line=True)


_PLACEHOLDER_PREFIX = "<!-- bgw fixture capture failed for"


def _has_real_fixture(fixture_root: str | Path, page: str) -> bool:
    """True when ``page`` already has a captured fixture that is not itself a failure placeholder."""
    path = Path(fixture_root) / FIXTURE_HTML_DIR / f"{page}.html"
    try:
        # Non-blocking and without following a link, so a FIFO or symlink in its place cannot hang the capture.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    except OSError:
        return True  # something is there that is not a plain file: it is not replaced by a placeholder
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return True
        with os.fdopen(descriptor, encoding="utf-8", errors="replace", closefd=False) as handle:
            head = handle.read(len(_PLACEHOLDER_PREFIX))
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return head != _PLACEHOLDER_PREFIX


def capture_fixture_pack(
    pages: list[SweepPage],
    fixture_root: str | Path,
    *,
    stdout: TextIO | None = None,
    degraded: list[str] | None = None,
) -> int:
    """Sanitize raw sweep output (include_raw + include_parsed, no fallbacks) into a fixture pack.

    Returns the number of pages whose real HTML was captured; pages without HTML get a
    placeholder comment plus an `error` receipt. Fails closed if sensitive residue survives.

    A page that failed this run never replaces a real fixture that is already there: its previous
    fixture is kept and its name is appended to ``degraded`` (and named in the closing summary
    line). A failed page with no previous fixture still gets the placeholder and receipt.
    """
    out = stdout if stdout is not None else sys.stdout
    kept: list[str] = degraded if degraded is not None else []
    captured = 0
    # An unusable fixture root is refused before any page is sanitized or written.
    sweep._preflight_output_tree(Path(fixture_root), [], [])
    for result in pages:
        page = result.page
        out.write(f"capturing {page}... ")
        refusal: str | None = None
        html = parsed = None
        if result.raw_html and not (not result.ok and _has_real_fixture(fixture_root, page)):
            try:
                secrets = fixture_secret_values(result.raw_html)
                html = sanitize_router_fixture(result.raw_html)
                parsed = _parse_page(page, html)
                _assert_fixture_safe(page, html, parsed, secrets=secrets)
            except (FixtureSafetyError, FixtureBoundsError) as error:
                # Never certify a page as redacted while a recognisable secret survives, and never
                # run unbounded on a page past the parser's bounds: the page is not captured.
                refusal, html, parsed = str(error), None, None
        if refusal is not None:
            result = replace(result, ok=False, error=refusal, raw_html=None)
        if not result.ok and _has_real_fixture(fixture_root, page):
            kept.append(page)
            out.write(f"failed, previous fixture kept: {_line(result.error or 'unusable response')}\n")
        elif result.raw_html and html is not None and parsed is not None:
            data_count = sweep._parsed_data_count(parsed)
            page_loads = result.ok and data_count > 0 and not is_junk_title(parsed.title or parsed.heading)
            expected = expected_fixture(page, parsed, page_loads=page_loads, data_count=data_count)
            error = sanitize_router_fixture(result.error) if result.error else None
            write_fixture_set(fixture_root, page, html, parsed, expected, error=error)
            captured += 1
            out.write("ok\n" if result.ok else f"captured error page: {_line(result.error or 'unusable response')}\n")
        else:
            message = result.error or "Router did not return page HTML."
            out.write(f"failed: {_line(message)}\n")
            html = f"<!-- bgw fixture capture failed for {page}: {sanitize_router_fixture(message)} -->\n"
            parsed = _parse_page(page, html)
            expected = expected_fixture(page, parsed, page_loads=False)
            write_fixture_set(fixture_root, page, html, parsed, expected, error=sanitize_router_fixture(message))
    out.write(f"captured {captured}/{len(pages)} router pages\n")
    if kept:
        out.write(f"degraded (previous fixture kept): {', '.join(kept)}\n")
    return captured


def _parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    return sweep._parse_page(page, html, include_secrets=include_secrets)


def _assert_fixture_safe(page: str, html: str, parsed: ParsedPage, *, secrets: list[str] | None = None) -> None:
    """Fail closed. The redacted parse already shows `[redacted]` for every secret the parser
    recognises, so control values are checked in an include_secrets parse of the sanitized HTML; an
    independent literal pass looks for every secret value found in the original (``secrets``)."""
    serialized = json.dumps(to_json_dict(parsed), separators=(",", ":"))
    unredacted = _parse_page(page, html, include_secrets=True)
    if (
        contains_sensitive_fixture_value(html)
        or contains_sensitive_fixture_value(serialized, html=False)
        or sensitive_control_residue(unredacted)
        or any(secret in html or secret in serialized for secret in secrets or ())
    ):
        raise FixtureSafetyError(f"Refusing to write {page} because sensitive fixture data remains after sanitization.")


__all__ = [
    "AuditResult",
    "ExpectedFixture",
    "FixtureCounts",
    "FixtureSafetyError",
    "build_audit",
    "capture_fixture_pack",
    "expected_fixture",
    "preflight_fixture_root",
    "write_fixture_set",
]
