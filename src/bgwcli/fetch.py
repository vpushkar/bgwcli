"""Fetch a router page and parse it, tolerating page-level failures (port of BGW320-CLI src/fetch.ts)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .client import PageGetter
from .errors import RouterAuthError, RouterSessionPoolFullError
from .types import ParsedPage

_PAGE_NOT_FOUND = re.compile(r"^Page not found\.?$", re.I)
LOGIN_PAGE_ERROR = "Router returned the login page instead of the requested page."


@dataclass
class ParsedPageResult:
    page: str
    ok: bool
    status_code: int | None = None
    parsed: ParsedPage | None = None
    error: str | None = None


def fetch_parsed_page(client: PageGetter, page: str, include_secrets: bool = False) -> ParsedPageResult:
    """GET + parse ``page``. Auth failures (incl. pool-full) propagate; anything else becomes ok=False."""
    from .parser import looks_like_login, parse_page

    try:
        response = client.get_cgi_page(page)
        parsed = parse_page(page, response.body, include_secrets=include_secrets)
        if looks_like_login(response.body):
            return ParsedPageResult(page, False, response.status_code, parsed, LOGIN_PAGE_ERROR)
        if _is_page_not_found(parsed):
            error = parsed.heading or parsed.title or "Page not found"
            return ParsedPageResult(page, False, response.status_code, parsed, error)
        return ParsedPageResult(page, 200 <= response.status_code < 400, response.status_code, parsed)
    except (RouterAuthError, RouterSessionPoolFullError):
        raise
    except Exception as error:  # noqa: BLE001 - per-page failures are reported, not raised (as in TS)
        return ParsedPageResult(page, False, error=str(error) or error.__class__.__name__)


def _is_page_not_found(parsed: ParsedPage) -> bool:
    return bool(_PAGE_NOT_FOUND.search(parsed.title) or _PAGE_NOT_FOUND.search(parsed.heading))


def parsed_data_count(parsed: ParsedPage) -> int:
    return (
        len(parsed.values)
        + len(parsed.tables)
        + len(parsed.fields)
        + len(parsed.selects)
        + len(parsed.textareas)
        + len(parsed.buttons)
        + len(parsed.forms)
    )
