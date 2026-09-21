"""Turn parsed router pages into a Snapshot: the structured truth that dump writes, diff compares
and restore writes back. Ported 1:1 from BGW320-CLI src/snapshot.ts.

Every "fail loudly" rule here exists because an understated snapshot is dangerous downstream:
an empty section makes diff report everything as missing/extra and lets `restore --prune`
delete what the dump was meant to preserve.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .errors import SnapshotExtractionError, UsageError
from .types import ParsedPage, ParsedSelect


@dataclass(frozen=True)
class SnapshotService:
    name: str
    ext_min_port: int
    ext_max_port: int
    int_start_port: int
    protocol: str


@dataclass(frozen=True)
class SnapshotForward:
    service: str
    device_label: str
    device_mac: str


@dataclass(frozen=True)
class SnapshotReservation:
    mac: str
    ip: str


@dataclass(frozen=True)
class SnapshotMeta:
    firmware: str
    ts: str
    router_host: str
    schema: int = 2


@dataclass(frozen=True)
class Snapshot:
    meta: SnapshotMeta
    services: list[SnapshotService] = field(default_factory=list)
    forwards: list[SnapshotForward] = field(default_factory=list)
    reservations: list[SnapshotReservation] = field(default_factory=list)
    forms: dict[str, dict[str, str]] = field(default_factory=dict)
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)


SNAPSHOT_PAGES: tuple[str, ...] = (
    "sysinfo",
    "services",
    "apphosting",
    "ipalloc",
    "packetfilter",
    "dosprotect",
    "wconfig",
    "wconfig_unified",
    "etherlan",
    "dhcpserver",
    "ippass",
    "wmacauth",
)
# Form pages restored field-by-field. Core pages are always captured by `dump`; optional pages
# only when named with `--include` (etherlan can cut the wire you are on, dhcpserver can move the
# gateway; ippass and wmacauth change how the LAN/Wi-Fi admits clients). dhcpserver (Subnets &
# DHCP), ippass (IP Passthrough) and wmacauth (Wi-Fi MAC Filtering modes) were added 2026-09-20
# for factory-reset recovery.
CORE_FORM_PAGES: tuple[str, ...] = ("dosprotect", "wconfig")
OPTIONAL_FORM_PAGES: tuple[str, ...] = ("etherlan", "dhcpserver", "ippass", "wmacauth")
FORM_PAGES: tuple[str, ...] = (*CORE_FORM_PAGES, *OPTIONAL_FORM_PAGES)
# wmacauth: the MAC filter list (Radio/Network/Filtering rows) is a table with Add/Remove semantics
# like packet filters, so it is recorded but not restored; only the mode selects are form fields.
# The etherlan/wmacauth tables travel with their (optional) form page.
_DOCUMENTARY_TABLE_PAGES: tuple[str, ...] = ("packetfilter", "ipalloc", "etherlan", "wmacauth")

IPV4_PATTERN = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
MAC_PATTERN = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)

# Recorded in `forms.<page>` for checkbox/radio controls that are not checked (see _form_values).
UNCHECKED = "<unchecked>"

_IGNORED_INPUT_TYPES = frozenset({"button", "submit", "reset", "image"})

# Column alias tables: normalized (lower-case alphanumeric) header names that identify a column.
SERVICE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "name": ("service", "servicename", "name"),
    "extMinPort": ("extminport", "externalportstart", "globalportrange", "portrange", "portstart", "fromport"),
    "extMaxPort": ("extmaxport", "externalportend", "portend", "toport"),
    "intStartPort": ("intstartport", "baseport", "basehostport", "internalportstart", "hostport", "mapto"),
    "protocol": ("protocol", "proto"),
}
FORWARD_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "service": ("service", "application", "servicename"),
    "deviceLabel": ("device", "neededbydevice", "neededby", "hostname", "name"),
}
RESERVATION_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "name": ("ipv4addressname", "ipv4address", "address", "ipaddress"),
    "mac": ("macaddress", "mac"),
    "allocation": ("allocation", "type"),
}


def split_include(value: str | Sequence[str] | None) -> list[str]:
    """Normalize a `--include` value (csv string or already-split list) to a list of page ids."""
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else [part for name in value for part in name.split(",")]
    return [name.strip() for name in parts if name.strip()]


def resolve_include(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """`dump --include <csv|all>` -> the optional form pages to capture, in OPTIONAL_FORM_PAGES order.

    Core page names are accepted and ignored (they are always captured); anything else is a usage error.
    """
    names = split_include(value)
    if not names:
        return ()
    if len(names) == 1 and names[0].lower() == "all":
        return OPTIONAL_FORM_PAGES
    unknown = [name for name in names if name not in FORM_PAGES]
    if unknown:
        raise UsageError(
            f"--include: unknown page(s) {', '.join(unknown)}; valid optional pages are "
            f"{', '.join(OPTIONAL_FORM_PAGES)} (or all)"
        )
    return tuple(page for page in OPTIONAL_FORM_PAGES if page in names)


def snapshot_pages_for(include: Iterable[str]) -> tuple[str, ...]:
    """Pages `dump` fetches: every snapshot page except the optional form pages not included."""
    included = set(include)
    return tuple(page for page in SNAPSHOT_PAGES if page not in OPTIONAL_FORM_PAGES or page in included)


def extract_snapshot(
    pages: Mapping[str, ParsedPage],
    *,
    ts: str,
    router_host: str,
    all_clients: bool = False,
    include: Iterable[str] = (),
) -> Snapshot:
    included = set(include)
    captured_forms = [page for page in FORM_PAGES if page in CORE_FORM_PAGES or page in included]
    forms: dict[str, dict[str, str]] = {}
    for page in captured_forms:
        parsed = pages.get(page)
        if parsed is not None:
            forms[page] = _form_values(parsed)
    tables: dict[str, list[dict[str, str]]] = {}
    for page in _DOCUMENTARY_TABLE_PAGES:
        parsed = pages.get(page)
        if parsed is None:
            continue
        # etherlan/wmacauth tables document an optional form page; they are only kept with it.
        if page in OPTIONAL_FORM_PAGES and page not in included:
            continue
        rows = [dict(row) for row in parsed.tables]
        if page == "packetfilter":
            rows.extend({"button": b.label} for b in parsed.buttons if b.label)
        # By default the documentary IP Allocation table keeps only Fixed Allocation rows, so the dump
        # carries no trace of DHCP-only devices; --all-clients keeps every row. `reservations` (what
        # diff/restore use) is fixed-rows-only regardless.
        if page == "ipalloc" and not all_clients:
            rows = [row for row in rows if _is_fixed_allocation_row(row)]
        tables[page] = rows
    return Snapshot(
        meta=SnapshotMeta(schema=2, firmware=_firmware_version(pages.get("sysinfo")), ts=ts, router_host=router_host),
        services=_extract_services(pages["services"]) if "services" in pages else [],
        forwards=_extract_forwards(pages["apphosting"]) if "apphosting" in pages else [],
        reservations=_extract_reservations(pages["ipalloc"]) if "ipalloc" in pages else [],
        forms=forms,
        tables=tables,
    )


def reservation_key(reservation: SnapshotReservation) -> str:
    return reservation.mac.lower()


def service_key(service: SnapshotService) -> str:
    return (
        f"{service.name}|{service.protocol}|{service.ext_min_port}-{service.ext_max_port}|{service.int_start_port}"
    ).lower()


def forward_key(forward: SnapshotForward) -> str:
    return f"{forward.service}|{forward.device_mac}".lower()


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def find_column(row: Mapping[str, str], aliases: Sequence[str]) -> str | None:
    for key in row:
        if _normalize(key) in aliases:
            return key
    return None


def _require_column(page: str, row: Mapping[str, str], field_name: str, aliases: Sequence[str]) -> str:
    key = find_column(row, aliases)
    if key is None:
        raise SnapshotExtractionError(f"{page}: column {field_name} not found; headers were [{', '.join(row.keys())}]")
    return key


# parse_page flattens every >=3-column table on a page into one row list, so rows without the
# identifying column are tolerated — but if the page has rows and NONE of them carry it, the
# header was renamed (firmware update) and silently returning [] would understate the router's
# state to both diff and restore --prune.
def _require_recognized_rows(page: str, rows: Sequence[Mapping[str, str]], recognized: int, fields: str) -> None:
    if rows and recognized == 0:
        raise SnapshotExtractionError(
            f"{page}: none of {len(rows)} table rows carry {fields} column(s); "
            f"headers were [{', '.join(rows[0].keys())}]"
        )


_LEADING_INT = re.compile(r"^[+-]?\d+")


def _parse_port(value: str, page: str, field_name: str) -> int:
    # Mirrors Number.parseInt(value.trim(), 10): leading integer digits, anything else is NaN.
    match = _LEADING_INT.match(value.strip())
    if match is None:
        raise SnapshotExtractionError(f"{page}: {field_name} '{value}' is not a port number")
    return int(match.group(0))


def _is_fixed_allocation_row(row: Mapping[str, str]) -> bool:
    key = find_column(row, RESERVATION_COLUMNS["allocation"])
    return key is not None and re.search("fixed", row.get(key, ""), re.IGNORECASE) is not None


def _extract_reservations(parsed: ParsedPage) -> list[SnapshotReservation]:
    reservations: list[SnapshotReservation] = []
    recognized = 0
    for row in parsed.tables:
        allocation_key = find_column(row, RESERVATION_COLUMNS["allocation"])
        mac_key = find_column(row, RESERVATION_COLUMNS["mac"])
        name_key = find_column(row, RESERVATION_COLUMNS["name"])
        if allocation_key is None or mac_key is None or name_key is None:
            continue
        recognized += 1
        if not re.search("fixed", row.get(allocation_key, ""), re.IGNORECASE):
            continue
        mac = row.get(mac_key, "").strip().lower()
        ip = row.get(name_key, "").strip()
        if not MAC_PATTERN.match(mac):
            raise SnapshotExtractionError(f"ipalloc: fixed allocation row has an invalid MAC ('{mac}')")
        if not IPV4_PATTERN.match(ip):
            raise SnapshotExtractionError(
                f"ipalloc: fixed allocation row for {mac} has no IPv4 address in the name column ('{ip}')"
            )
        reservations.append(SnapshotReservation(mac=mac, ip=ip))
    _require_recognized_rows("ipalloc", parsed.tables, recognized, "allocation/mac/name")
    return reservations


def _extract_services(parsed: ParsedPage) -> list[SnapshotService]:
    services: list[SnapshotService] = []
    recognized = 0
    for row in parsed.tables:
        name_key = find_column(row, SERVICE_COLUMNS["name"])
        if name_key is None:
            continue
        recognized += 1
        name = row.get(name_key, "").strip()
        if not name:
            continue
        min_key = _require_column("services", row, "extMinPort", SERVICE_COLUMNS["extMinPort"])
        raw_min = row.get(min_key, "")
        if "-" in raw_min:
            lo, hi = raw_min.split("-", 1)
            ext_min = _parse_port(lo, "services", "extMinPort")
            ext_max = _parse_port(hi if hi else lo, "services", "extMaxPort")
        else:
            ext_min = _parse_port(raw_min, "services", "extMinPort")
            max_key = find_column(row, SERVICE_COLUMNS["extMaxPort"])
            ext_max = _parse_port(row.get(max_key, ""), "services", "extMaxPort") if max_key else ext_min
        int_key = _require_column("services", row, "intStartPort", SERVICE_COLUMNS["intStartPort"])
        protocol_key = _require_column("services", row, "protocol", SERVICE_COLUMNS["protocol"])
        services.append(
            SnapshotService(
                name=name,
                ext_min_port=ext_min,
                ext_max_port=ext_max,
                int_start_port=_parse_port(row.get(int_key, ""), "services", "intStartPort"),
                protocol=row.get(protocol_key, "").strip().upper(),
            )
        )
    _require_recognized_rows("services", parsed.tables, recognized, "name")
    return services


def _device_select(parsed: ParsedPage) -> ParsedSelect | None:
    for s in parsed.selects:
        if _normalize(s.name) == "device":
            return s
    for s in parsed.selects:
        details = s.option_details or []
        if details and all(MAC_PATTERN.match(o.value) for o in details):
            return s
    return None


def _extract_forwards(parsed: ParsedPage) -> list[SnapshotForward]:
    select = _device_select(parsed)
    macs_by_label: dict[str, list[str]] = {}
    for option in (select.option_details if select else None) or []:
        macs_by_label.setdefault(option.label.strip(), []).append(option.value.lower())
    forwards: list[SnapshotForward] = []
    recognized = 0
    for row in parsed.tables:
        service_col = find_column(row, FORWARD_COLUMNS["service"])
        device_col = find_column(row, FORWARD_COLUMNS["deviceLabel"])
        if service_col is None or device_col is None:
            continue
        recognized += 1
        service = row.get(service_col, "").strip()
        device_label = row.get(device_col, "").strip()
        if not service or not device_label:
            continue
        macs = macs_by_label.get(device_label, [])
        # Both failure modes are hard errors. Ambiguous: two devices share a name, so there is no
        # way to know which MAC the forward belongs to. Unknown: recording an unresolvable label
        # with an empty MAC would make forward_key ('service|') stop matching the same live forward
        # once the device reappears ('service|<mac>'), so the diff would report it as BOTH missing
        # and extra — and `restore --prune` would delete the forward the dump was meant to preserve
        # while its compensating add stayed blocked. A dump must never contain an unresolved device.
        if len(macs) > 1:
            raise SnapshotExtractionError(
                f"apphosting: device label '{device_label}' is ambiguous ({len(macs)} devices); "
                "cannot dump forwards safely"
            )
        if not macs:
            raise SnapshotExtractionError(
                f"apphosting: device label '{device_label}' (service '{service}') is not in the router's device "
                "list; reconnect the device or delete that forward in the gateway UI, then re-run dump"
            )
        forwards.append(SnapshotForward(service=service, device_label=device_label, device_mac=macs[0]))
    _require_recognized_rows("apphosting", parsed.tables, recognized, "service/device")
    return forwards


# Mirrors base_payload in mutations.py (disabled controls omitted) with TWO deliberate exceptions:
#  - WPS PIN submit fields (wpspin*) are actions, not configuration — excluded from the dump while
#    base_payload still sends their live value on restore, as a browser would.
#  - Unchecked checkbox/radio controls are recorded as UNCHECKED instead of omitted. base_payload
#    omits them (that is how a browser posts an unchecked box), but a dump that omitted them could
#    not tell "the owner had this off" from "this field did not exist yet", so restore could never
#    re-uncheck a box. A radio group keeps its checked member; unchecked siblings never overwrite it.
# Controls that live on a form page but are not configuration: wmacauth's add-a-MAC sub-form (its
# results are the documentary filter-list table). Excluded from dumps and never restored.
_FORM_FIELD_EXCLUDES: dict[str, tuple[str, ...]] = {
    "wmacauth": ("macaddress", "maclist", "ssid11", "ssid12", "ssid21"),
}


def _excluded_field(page: str, name: str) -> bool:
    return name in _FORM_FIELD_EXCLUDES.get(page, ())


def _form_values(parsed: ParsedPage) -> dict[str, str]:
    values: dict[str, str] = {}
    unchecked: list[str] = []
    for f in parsed.fields:
        if _excluded_field(parsed.page, f.name):
            continue
        if f.name in ("nonce", "hashpassword"):
            continue
        if _normalize(f.name).startswith("wpspin"):
            continue  # WPS PIN submit field is an action, not configuration
        if f.disabled:
            continue
        if f.type.lower() in _IGNORED_INPUT_TYPES:
            continue
        if f.type in ("checkbox", "radio"):
            # A checkable control whose submit value IS the sentinel would record the same string
            # checked and unchecked, so saved-off vs live-on would diff as identical. Refuse at capture.
            if f.value == UNCHECKED:
                raise SnapshotExtractionError(
                    f"{parsed.page}: control '{f.name}' has submit value '{UNCHECKED}', which collides with the "
                    "unchecked sentinel; cannot dump this page safely"
                )
            if not f.checked:
                unchecked.append(f.name)
                continue
        values[f.name] = f.value
    for name in unchecked:
        if name not in values:
            values[name] = UNCHECKED
    for s in parsed.selects:
        if _excluded_field(parsed.page, s.name):
            continue
        if not s.disabled:
            values[s.name] = s.value
    for t in parsed.textareas:
        if not t.disabled:
            values[t.name] = t.value
    return values


def _firmware_version(parsed: ParsedPage | None) -> str:
    if parsed is None:
        return ""
    for key, value in parsed.values.items():
        if _normalize(key) in ("softwareversion", "firmwareversion"):
            return value.strip()
    return ""
