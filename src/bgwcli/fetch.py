"""Fetch a router page and parse it, tolerating page-level failures (port of BGW320-CLI src/fetch.ts)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .client import PageGetter
from .errors import RouterAuthError, is_page_level_auth_error
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
    # The gateway answered, but not with the page: a Login page, "Page not found", a table-backed
    # page without its table structure. Unlike a transport failure it says something about the
    # gateway's state, so unattended callers must not treat it as "router unreachable".
    structural: bool = field(default=False, metadata={"serialize": False})


def fetch_parsed_page(client: PageGetter, page: str, include_secrets: bool = False) -> ParsedPageResult:
    """GET + parse ``page``. Login failures, login-page bounces and pool-full propagate; a 401/403 answered
    by this page alone, and anything else, becomes ok=False."""
    from .parser import looks_like_login, parse_page

    try:
        response = client.get_cgi_page(page)
        parsed = parse_page(page, response.body, include_secrets=include_secrets)
        # BGW320Client.get_cgi_page never returns a Login page (it re-logs in or raises), but this
        # accepts any PageGetter: other getters (wrappers, injected fakes) can hand one back.
        if looks_like_login(response.body):
            return ParsedPageResult(page, False, response.status_code, parsed, LOGIN_PAGE_ERROR, structural=True)
        if is_page_not_found(parsed):
            error = parsed.heading or parsed.title or "Page not found"
            return ParsedPageResult(page, False, response.status_code, parsed, error, structural=True)
        return ParsedPageResult(page, 200 <= response.status_code < 400, response.status_code, parsed)
    except RouterAuthError as error:  # pool-full is a RouterAuthError and is never page-level
        if not is_page_level_auth_error(error):
            raise
        # One page answering 401/403 is that page's failure; the session is not known to be lost.
        return ParsedPageResult(page, False, status_code=error.status_code, error=str(error))
    except Exception as error:  # noqa: BLE001 - per-page failures are reported, not raised (as in TS)
        return ParsedPageResult(page, False, error=str(error) or error.__class__.__name__)


def is_page_not_found(parsed: ParsedPage) -> bool:
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
