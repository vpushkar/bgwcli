"""Connected-device listing with the IP Allocation fallback. Port of src/devices.ts.

`devices.ha` is known to hang on the BGW320; when it fails with anything other than an auth error the
list is rebuilt (degraded: no connection/last-activity info) from the ipalloc table rows.
--limit is applied by the renderer (format.py), exactly as in the TS CLI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .errors import RouterAuthError, RouterSessionPoolFullError
from .types import Device

_ONLINE = re.compile(r"on", re.IGNORECASE)
_IP_NAME_SEPARATOR = re.compile(r"\s+/\s+")

DevicesParser = Callable[[str], list[Device]]
# (client, page, include_secrets) -> fetch.ParsedPageResult-like (.ok, .parsed, .error)
PageFetcher = Callable[..., Any]


@dataclass
class DeviceListResult:
    fallback: bool
    devices: list[Device] = field(default_factory=list)
    error: str | None = None


def fetch_device_list(
    client: Any,
    include_offline: bool = False,
    *,
    parse_devices: DevicesParser | None = None,
    fetch_parsed_page: PageFetcher | None = None,
) -> DeviceListResult:
    """GET devices.ha and parse it; on non-auth failure fall back to the ipalloc table.

    `parse_devices` / `fetch_parsed_page` default to bgwcli.parser.parse_devices and
    bgwcli.fetch.fetch_parsed_page (imported lazily); tests inject fakes.
    """
    try:
        response = client.get_cgi_page("devices")
        if parse_devices is None:
            from .parser import parse_devices as _parse_devices

            parse_devices = _parse_devices
        return DeviceListResult(fallback=False, devices=filter_devices(parse_devices(response.body), include_offline))
    except RouterAuthError:
        raise
    except Exception as error:  # noqa: BLE001 - any transport/parse failure degrades to the fallback
        message = str(error)
        fallback = _fetch_parsed_page_soft(client, "ipalloc", fetch_parsed_page)
        if fallback is None or not fallback.ok or fallback.parsed is None:
            return DeviceListResult(fallback=True, devices=[], error=message)
        devices = filter_devices(devices_from_ip_allocation_rows(fallback.parsed.tables), include_offline)
        return DeviceListResult(fallback=True, devices=devices, error=message)


def _fetch_parsed_page_soft(client: Any, page: str, fetch_parsed_page: PageFetcher | None):
    """Like fetch.fetch_parsed_page but an auth error during the fallback is a soft failure (None)."""
    if fetch_parsed_page is None:
        from .fetch import fetch_parsed_page as _fetch_parsed_page

        fetch_parsed_page = _fetch_parsed_page
    try:
        return fetch_parsed_page(client, page)
    except RouterSessionPoolFullError:
        # A full session pool is never a soft failure: swallowing it here would return an empty
        # device list with exit 0 and the coordinator would never record the cooldown.
        raise
    except RouterAuthError:
        return None


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
    """Default view keeps only devices whose status mentions 'on' (case-insensitive); --all keeps everything."""
    if include_offline:
        return list(devices)
    return [device for device in devices if _ONLINE.search(device.status)]


def split_ip_and_name(value: str) -> tuple[str, str]:
    """'192.168.1.20 / laptop' -> ('192.168.1.20', 'laptop'); splits once on a whitespace-padded slash."""
    parts = _IP_NAME_SEPARATOR.split(value, maxsplit=1)
    ip = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    return ip.strip(), name.strip()
