"""Status view models: device status (home), home network (lanstatistics), security options, status sections.

Port of src/status.ts. Each composite view tries one primary page and, when it is unusable, falls
back to a fixed list of narrower pages. Fallback lists are intentionally narrow (see `bgw --help`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import RouterAuthError
from .fetch import parsed_data_count
from .types import ParsedPage

__all__ = [
    "FALLBACK_DEVICE_STATUS_PAGES",
    "FALLBACK_HOME_NETWORK_PAGES",
    "FALLBACK_SECURITY_OPTIONS_PAGES",
    "FALLBACK_STATUS_PAGES",
    "StatusResult",
    "StatusSection",
    "fetch_device_status",
    "fetch_home_network_status",
    "fetch_security_options",
    "fetch_status_sections",
    "parsed_data_count",
]

UNUSABLE_RESPONSE = "Router returned an unusable response."

FALLBACK_DEVICE_STATUS_PAGES: tuple[str, ...] = ("sysinfo", "broadbandstatistics", "firewall")
FALLBACK_STATUS_PAGES: tuple[str, ...] = ("sysinfo", "broadbandstatistics", "fiberstat", "firewall")
FALLBACK_HOME_NETWORK_PAGES: tuple[str, ...] = ("etherlan", "dhcpserver", "ipalloc", "wconfig_unified")
FALLBACK_SECURITY_OPTIONS_PAGES: tuple[str, ...] = ("firewall", "dosprotect")

# (client, page, include_secrets) -> fetch.ParsedPageResult-like (.ok, .status_code, .parsed, .error)
PageFetcher = Callable[..., Any]


@dataclass
class StatusSection:
    page: str
    ok: bool
    title: str | None = None
    heading: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    tables: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


@dataclass
class StatusResult:
    """DeviceStatusResult / CompositeStatusResult in the TS CLI share this shape."""

    page: str
    fallback: bool
    status_code: int | None = None
    title: str | None = None
    parsed: ParsedPage | None = None
    error: str | None = None
    sections: list[StatusSection] = field(default_factory=list)


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    status_code: int | None
    parsed: ParsedPage | None
    error: str | None


def fetch_device_status(
    client: Any, include_secrets: bool = False, *, fetch_parsed_page: PageFetcher | None = None
) -> StatusResult:
    return _fetch_composite(client, "home", FALLBACK_DEVICE_STATUS_PAGES, include_secrets, fetch_parsed_page)


def fetch_home_network_status(
    client: Any, include_secrets: bool = False, *, fetch_parsed_page: PageFetcher | None = None
) -> StatusResult:
    return _fetch_composite(client, "lanstatistics", FALLBACK_HOME_NETWORK_PAGES, include_secrets, fetch_parsed_page)


def fetch_security_options(
    client: Any, include_secrets: bool = False, *, fetch_parsed_page: PageFetcher | None = None
) -> StatusResult:
    return _fetch_composite(
        client, "securityoptions", FALLBACK_SECURITY_OPTIONS_PAGES, include_secrets, fetch_parsed_page
    )


def fetch_status_sections(
    client: Any, include_secrets: bool = False, *, fetch_parsed_page: PageFetcher | None = None
) -> list[StatusSection]:
    """The `status` command: sysinfo, broadbandstatistics, fiberstat, firewall as independent sections."""
    return _fetch_sections(client, FALLBACK_STATUS_PAGES, include_secrets, fetch_parsed_page)


def _fetch_composite(
    client: Any,
    primary: str,
    fallback_pages: Sequence[str],
    include_secrets: bool,
    fetch_parsed_page: PageFetcher | None,
) -> StatusResult:
    outcome = _fetch_soft(client, primary, include_secrets, fetch_parsed_page)
    if outcome.ok and outcome.parsed is not None and parsed_data_count(outcome.parsed) > 0:
        parsed = outcome.parsed
        return StatusResult(
            page=primary,
            fallback=False,
            status_code=outcome.status_code,
            title=parsed.title,
            parsed=parsed,
            sections=[_ok_section(primary, parsed)],
        )
    return StatusResult(
        page=primary,
        fallback=True,
        error=outcome.error or UNUSABLE_RESPONSE,
        sections=_fetch_sections(client, fallback_pages, include_secrets, fetch_parsed_page),
    )


def _fetch_sections(
    client: Any, pages: Sequence[str], include_secrets: bool, fetch_parsed_page: PageFetcher | None
) -> list[StatusSection]:
    sections: list[StatusSection] = []
    for page in pages:
        outcome = _fetch_soft(client, page, include_secrets, fetch_parsed_page)
        if outcome.ok and outcome.parsed is not None:
            sections.append(_ok_section(page, outcome.parsed))
        else:
            sections.append(StatusSection(page=page, ok=False, error=outcome.error or UNUSABLE_RESPONSE))
    return sections


def _ok_section(page: str, parsed: ParsedPage) -> StatusSection:
    return StatusSection(
        page=page, ok=True, title=parsed.title, heading=parsed.heading, values=parsed.values, tables=parsed.tables
    )


def _fetch_soft(client: Any, page: str, include_secrets: bool, fetch_parsed_page: PageFetcher | None) -> _Outcome:
    """fetch.fetch_parsed_page, but any non-auth exception becomes a failed outcome instead of aborting."""
    if fetch_parsed_page is None:
        from .fetch import fetch_parsed_page as _fetch_parsed_page

        fetch_parsed_page = _fetch_parsed_page
    try:
        result = fetch_parsed_page(client, page, include_secrets)
    except RouterAuthError:
        raise
    except Exception as error:  # noqa: BLE001 - status views degrade, they do not abort
        return _Outcome(ok=False, status_code=None, parsed=None, error=str(error))
    return _Outcome(
        ok=bool(result.ok),
        status_code=getattr(result, "status_code", None),
        parsed=getattr(result, "parsed", None),
        error=getattr(result, "error", None),
    )
