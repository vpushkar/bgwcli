"""Connected-device listing with the IP Allocation fallback. Port of src/devices.ts.

`devices.ha` is known to hang on the BGW320; when it fails with anything other than a session-wide
auth error (a 401/403 answered by devices.ha alone is that page's failure) the list is rebuilt
(degraded: no connection/last-activity info) from the ipalloc table rows. During that fallback a
lost router session (failed login, login-page bounce, full session pool) propagates like everywhere
else. When ipalloc cannot be read either (it answers 401/403 itself, or the fetch fails) nothing was
read: the result carries `fallback_error` and the CLI exits 2. A fallback that was read and lists
zero devices is an answer (exit 0) only when the ipalloc page carries the IP Allocation table header
(IPv4 Address / Name, MAC Address): a header-only table is an empty list. An HTTP 200 "Page not found"
document, or a body without the device table header (devices.ha) / the IP Allocation header (ipalloc),
is an unavailable page, not an empty list: on devices.ha it triggers the fallback, on ipalloc it
leaves nothing read. A page the parser cut at one of its bounds is
unavailable in the same way (its list would be partial).
--limit is applied by the renderer (format.py), exactly as in the TS CLI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any

from .errors import RouterAuthError, is_page_level_auth_error
from .types import Device

_IP_NAME_SEPARATOR = re.compile(r"\s+/(?:\s+|$)")

DevicesParser = Callable[[str], list[Device]]
# (client, page, include_secrets) -> fetch.ParsedPageResult-like (.ok, .parsed, .error)
PageFetcher = Callable[..., Any]


@dataclass
class DeviceListResult:
    fallback: bool
    devices: list[Device] = field(default_factory=list)
    error: str | None = None
    # Set when devices.ha failed and the ipalloc fallback could not be read either.
    fallback_error: str | None = None

    @property
    def unread(self) -> bool:
        """True when neither devices.ha nor the ipalloc fallback was read: no answer."""
        return self.fallback_error is not None


def fetch_device_list(
    client: Any,
    include_offline: bool = False,
    *,
    parse_devices: DevicesParser | None = None,
    fetch_parsed_page: PageFetcher | None = None,
) -> DeviceListResult:
    """GET devices.ha and parse it; on any failure but a session-wide auth error fall back to the ipalloc table.

    `parse_devices` / `fetch_parsed_page` default to bgwcli.parser.parse_devices and
    bgwcli.fetch.fetch_parsed_page (imported lazily); tests inject fakes.
    """
    try:
        response = client.get_cgi_page("devices")
        _raise_if_unusable(response.body)
        if parse_devices is None:
            from .parser import parse_devices as _parse_devices

            parse_devices = _parse_devices
        return DeviceListResult(fallback=False, devices=filter_devices(parse_devices(response.body), include_offline))
    except RouterAuthError as error:
        # devices.ha alone answering 401/403 is that page's failure; the session-wide ones re-raise.
        if not is_page_level_auth_error(error):
            raise
        message = str(error)
    except Exception as error:  # noqa: BLE001 - any transport/parse failure degrades to the fallback
        message = str(error)
    recorder = _BodyRecorder(client)
    fallback, unavailable = _fetch_parsed_page_soft(recorder, "ipalloc", fetch_parsed_page)
    if fallback is not None and fallback.ok and getattr(fallback.parsed, "truncated", None):
        reason = "ipalloc: the page was cut by the parser's bounds; its device table is not complete"
        return DeviceListResult(fallback=True, devices=[], error=message, fallback_error=reason)
    body = recorder.body
    if fallback is not None and fallback.ok and body is not None and not has_ip_allocation_header(body):
        reason = "ipalloc: the response has no IP Allocation table (IPv4 Address / Name, MAC Address)"
        return DeviceListResult(fallback=True, devices=[], error=message, fallback_error=reason)
    if fallback is None or not fallback.ok or fallback.parsed is None:
        reason = unavailable or getattr(fallback, "error", None) or "ipalloc unavailable"
        return DeviceListResult(fallback=True, devices=[], error=message, fallback_error=reason)
    devices = filter_devices(devices_from_ip_allocation_rows(fallback.parsed.tables), include_offline)
    return DeviceListResult(fallback=True, devices=devices, error=message)


class _BodyRecorder:
    """Hands page reads through to `client` and keeps the last body: the parsed page has no table
    header once the table has no rows, and the header is what tells an empty list from a blank page."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.body: str | None = None

    def get_cgi_page(self, *args: Any, **kwargs: Any) -> Any:
        response = self._client.get_cgi_page(*args, **kwargs)
        self.body = response.body
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def has_ip_allocation_header(body: str) -> bool:
    """True when `body` carries the IP Allocation table: a header naming both `IPv4 Address / Name`
    and `MAC Address`, with or without rows."""
    from .parser import extract_tables

    wanted = ("ipv4 address / name", "mac address")
    for table in extract_tables(body):
        for row in table:
            cells = {" ".join(cell.lower().split()) for cell in row}
            if all(name in cells for name in wanted):
                return True
    return False


class _UnusablePage(Exception):
    """devices.ha answered 200 with something that is not a device list."""


def _raise_if_unusable(body: str) -> None:
    """A "Page not found" document, or any body without the device table header (a busy or error
    page), is an unavailable page, not an empty list: an empty list still carries the header."""
    from .fetch import is_page_not_found
    from .parser import has_device_table, parse_page

    parsed = parse_page("devices", body)
    if parsed.truncated:
        raise _UnusablePage("devices: the page was cut by the parser's bounds; its device list is not complete")
    if is_page_not_found(parsed):
        raise _UnusablePage(f"devices: {parsed.heading or parsed.title or 'Page not found'}")
    if not has_device_table(body):
        raise _UnusablePage("devices: the response has no device table")


def _fetch_parsed_page_soft(
    client: Any, page: str, fetch_parsed_page: PageFetcher | None
) -> tuple[Any | None, str | None]:
    """Like fetch.fetch_parsed_page but the page itself answering 401/403 is a soft failure:
    returns (result, None), or (None, the 401/403 message).

    Session-wide auth errors (failed login, login-page bounce, full session pool) propagate: an
    empty device list would hide that the router session is gone, and a full pool must reach the
    coordinator so the cooldown is recorded.
    """
    if fetch_parsed_page is None:
        from .fetch import fetch_parsed_page as _fetch_parsed_page

        fetch_parsed_page = _fetch_parsed_page
    try:
        return fetch_parsed_page(client, page), None
    except RouterAuthError as error:
        if is_page_level_auth_error(error):
            return None, str(error)
        raise


def devices_from_ip_allocation_rows(rows: list[dict[str, str]]) -> list[Device]:
    """Degraded Device records from the IP Allocation table (no connection type / activity)."""
    devices: list[Device] = []
    for row in rows:
        ip, name = split_ip_and_name(row.get("IPv4 Address / Name", ""))
        device = Device(
            status=row.get("Status", ""),
            name=name,
            ip=ip,
            mac=row.get("MAC Address", ""),
            connection="",
            allocation=row.get("Allocation"),
            last_activity="",
        )
        if device.ip or device.mac or device.name:
            devices.append(device)
    return devices


def filter_devices(devices: list[Device], include_offline: bool) -> list[Device]:
    """Default view keeps only devices whose status is exactly 'on' (case-insensitive, surrounding
    whitespace ignored), as the router reports it; --all keeps everything."""
    if include_offline:
        return list(devices)
    return [device for device in devices if device.status.strip().lower() == "on"]


def split_ip_and_name(value: str) -> tuple[str, str]:
    """'192.168.1.20 / laptop' -> ('192.168.1.20', 'laptop'); splits once on a whitespace-padded slash
    (a trailing one, '192.168.1.20 /', is an unnamed device).

    A value without that separator is an address only when it is an IPv4 or IPv6 address
    ('10.0.0.5' -> ('10.0.0.5', '')); anything else is the device name ('watch' -> ('', 'watch')),
    as the device-page parser reads the same column."""
    parts = _IP_NAME_SEPARATOR.split(value, maxsplit=1)
    if len(parts) > 1:
        return parts[0].strip(), parts[1].strip()
    single = value.strip()
    return (single, "") if _is_ip_address(single) else ("", single)


def _is_ip_address(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True
