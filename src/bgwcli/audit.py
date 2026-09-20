"""Health / usefulness summaries over sweep results.

`build_audit` powers `bgwcli audit` / `readiness`. `expected_fixture`, `write_fixture_set` and
`capture_fixture_pack` produce the gitignored router fixture pack (router-html/, parsed/,
expected/ under tests/fixtures) with the same evidence fields as the TS capture script.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from . import sweep
from .errors import BgwError
from .fixture_sanitize import contains_sensitive_fixture_value, sanitize_router_fixture
from .sweep import SweepPage, is_junk_title
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
        secrets_redacted=not contains_sensitive_fixture_value(serialized),
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
    """Write router-html/<page>.html, parsed/<page>.json, expected/<page>.json (mode 0600)."""
    root = Path(fixture_root)
    paths = [
        root / FIXTURE_HTML_DIR / f"{page}.html",
        root / FIXTURE_PARSED_DIR / f"{page}.json",
        root / FIXTURE_EXPECTED_DIR / f"{page}.json",
    ]
    expected_json = to_json_dict(expected)
    if error:
        expected_json["error"] = error
    contents = [
        html.rstrip() + "\n",
        json.dumps(to_json_dict(parsed), indent=2) + "\n",
        json.dumps(expected_json, indent=2) + "\n",
    ]
    for path, content in zip(paths, contents, strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o600)
    return paths


def capture_fixture_pack(pages: list[SweepPage], fixture_root: str | Path, *, stdout: TextIO | None = None) -> int:
    """Sanitize raw sweep output (include_raw + include_parsed, no fallbacks) into a fixture pack.

    Returns the number of pages whose real HTML was captured; pages without HTML get a
    placeholder comment plus an `error` receipt. Fails closed if sensitive residue survives.
    """
    out = stdout if stdout is not None else sys.stdout
    captured = 0
    for result in pages:
        page = result.page
        out.write(f"capturing {page}... ")
        if result.raw_html:
            html = sanitize_router_fixture(result.raw_html)
            parsed = _parse_page(page, html)
            _assert_fixture_safe(page, html, parsed)
            data_count = sweep._parsed_data_count(parsed)
            page_loads = result.ok and data_count > 0 and not is_junk_title(parsed.title or parsed.heading)
            expected = expected_fixture(page, parsed, page_loads=page_loads, data_count=data_count)
            error = sanitize_router_fixture(result.error) if result.error else None
            write_fixture_set(fixture_root, page, html, parsed, expected, error=error)
            captured += 1
            out.write("ok\n" if result.ok else f"captured error page: {result.error or 'unusable response'}\n")
        else:
            message = result.error or "Router did not return page HTML."
            out.write(f"failed: {message}\n")
            html = f"<!-- bgw fixture capture failed for {page}: {sanitize_router_fixture(message)} -->\n"
            parsed = _parse_page(page, html)
            expected = expected_fixture(page, parsed, page_loads=False)
            write_fixture_set(fixture_root, page, html, parsed, expected, error=sanitize_router_fixture(message))
    out.write(f"captured {captured}/{len(pages)} router pages\n")
    return captured


def _parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    return sweep._parse_page(page, html, include_secrets=include_secrets)


def _assert_fixture_safe(page: str, html: str, parsed: ParsedPage) -> None:
    serialized = json.dumps(to_json_dict(parsed), separators=(",", ":"))
    if contains_sensitive_fixture_value(html) or contains_sensitive_fixture_value(serialized):
        raise FixtureSafetyError(f"Refusing to write {page} because sensitive fixture data remains after sanitization.")


__all__ = [
    "AuditResult",
    "ExpectedFixture",
    "FixtureCounts",
    "FixtureSafetyError",
    "build_audit",
    "capture_fixture_pack",
    "expected_fixture",
    "write_fixture_set",
]
