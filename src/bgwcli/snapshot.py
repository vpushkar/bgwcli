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
from .html import normalize_whitespace
from .terminal import sanitize_terminal_text
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
class LiveFormEvidence:
    """HTML facts used only while comparing a snapshot from the same live capture."""

    legacy_default_names: frozenset[str] = frozenset()
    # Names the live page renders only as disabled controls (never posted, never read back).
    disabled_names: frozenset[str] = frozenset()
    # Names the live page renders as <textarea> (see legacy_form_spelling).
    textarea_names: frozenset[str] = frozenset()
    # Every named control the live page renders (disabled and non-configuration ones included). A
    # dumped name outside this set cannot be posted by a browser or read back on this firmware.
    rendered_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Snapshot:
    meta: SnapshotMeta
    services: list[SnapshotService] = field(default_factory=list)
    forwards: list[SnapshotForward] = field(default_factory=list)
    reservations: list[SnapshotReservation] = field(default_factory=list)
    forms: dict[str, dict[str, str]] = field(default_factory=dict)
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Per form page, the dumped field names the page rendered as type="password" controls. Field
    # names alone do not show that they are secret, and a dump keeps no control types, so diff and
    # restore output need this record to keep those values redacted. dumpfile writes it as
    # `formSecrets` only when non-empty; it is not part of the generic to_json_dict view.
    form_secrets: dict[str, list[str]] = field(default_factory=dict, metadata={"serialize": False})
    # Per form page, the field names whose value is the UNCHECKED string because the control is a
    # text/select/textarea holding that literal text, not a checkbox/radio that was off. Every other
    # UNCHECKED value is the off marker, which is also how a dump taken before this record existed
    # (or by the TypeScript CLI) reads. dumpfile writes it as `formUncheckedText` only when a page has
    # such a field, so every other dump keeps its byte-identical shape; it is not part of the generic
    # to_json_dict view.
    form_unchecked_text: dict[str, list[str]] = field(default_factory=dict, metadata={"serialize": False})
    # Keep this alongside forms when copying/projecting the same capture. Schema-2 JSON
    # has no control types or omitted-value facts, so a loaded dump cannot supply evidence.
    live_form_evidence: dict[str, LiveFormEvidence] = field(
        default_factory=dict, repr=False, compare=False, metadata={"serialize": False}
    )


SNAPSHOT_PAGES: tuple[str, ...] = (
    "sysinfo",
    "services",
    "apphosting",
    "ipalloc",
    "packetfilter",
    "dosprotect",
    "wconfig",
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
# Pages read only for a documentary record (never compared or restored): a failed read is a
# warning and the snapshot simply lacks that table, instead of aborting dump/diff/restore.
BEST_EFFORT_PAGES: tuple[str, ...] = ("packetfilter",)

_OCTET = r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
IPV4_PATTERN = re.compile(rf"^{_OCTET}(?:\.{_OCTET}){{3}}\Z")
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


# The column groups whose header row identifies each table-backed section. A page answered without
# such a header row (a "Please wait" page, a truncated body, a renamed header) did not show its table,
# so it is a failed read: reading it as an empty section would let diff report every row as missing
# and `restore --prune` delete rows the dump was meant to preserve. A header row without data rows
# is a legitimate empty section, and so is the gateway's one-cell empty table (`_EMPTY_TABLE_MARKERS`).
_TABLE_SECTIONS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    # Name alone is not enough for services: the page's entry form labels its first row "Service Name"
    # in a row header, which would pass for the table on a page whose table is missing.
    "services": (SERVICE_COLUMNS["name"], SERVICE_COLUMNS["extMinPort"]),
    "apphosting": (FORWARD_COLUMNS["service"], FORWARD_COLUMNS["deviceLabel"]),
    "ipalloc": (RESERVATION_COLUMNS["name"], RESERVATION_COLUMNS["mac"], RESERVATION_COLUMNS["allocation"]),
}
_TABLE_SECTION_LABELS: Mapping[str, str] = {
    "services": "custom services", "apphosting": "port forwards", "ipalloc": "IP allocation",
}
# With no entries the gateway (fw 6.35.8, observed live 2026-10-08) renders the section's table WITHOUT
# its header row: a single cell spanning the table that carries this sentence, followed by the entry
# form. That one-cell table is the section's legitimate empty state. The sentence is matched per
# section, normalized like a header: another section's sentence, or a reworded one, is not this table.
# The IP Allocation table lists every client, so it was never observed empty (fw 6.35.8) and has no
# marker; an ipalloc page without its header row stays a failed read.
_EMPTY_TABLE_MARKERS: Mapping[str, tuple[str, ...]] = {
    "services": ("nocustomserviceentrieshavebeendefined",),
    "apphosting": ("noapplicationhostingentrieshavebeendefined",),
}


def shows_empty_table_marker(page: str, html: str | None) -> bool:
    """Whether the raw `html` shows `page`'s one-cell "No ... entries have been defined" table: the
    gateway's rendering of the section with no entries (no header row), hence a readable EMPTY section.
    Only the exact one-row, one-cell shape with this section's own sentence counts."""
    markers = _EMPTY_TABLE_MARKERS.get(page, ())
    if not markers or not html:
        return False
    from .parser import extract_tables

    return any(
        len(table) == 1 and len(table[0]) == 1 and _normalize(table[0][0]) in markers for table in extract_tables(html)
    )


def missing_table_structure(page: str, parsed: ParsedPage | None, html: str | None) -> str | None:
    """Why a page read cannot be taken as `page`'s table section: None when a parsed row carries the
    section's columns, when the raw `html` shows the table's header row or the gateway's one-cell
    "No ... entries have been defined" table, or when the page has no table section."""
    required = _TABLE_SECTIONS.get(page)
    if required is None:
        return None
    if parsed is not None and any(
        all(find_column(row, aliases) is not None for aliases in required) for row in parsed.tables
    ):
        return None
    from .parser import extract_tables

    for table in extract_tables(html or ""):
        if not table:
            continue
        header = {_normalize(cell) for cell in table[0]}
        if all(header.intersection(aliases) for aliases in required):
            return None
    if shows_empty_table_marker(page, html):
        return None
    return (
        f"the page shows no {_TABLE_SECTION_LABELS[page]} table (no header row with the expected "
        "columns, nor the gateway's \"No ... entries have been defined\" cell); it is not read as an "
        "empty section"
    )


def missing_form_controls(page: str, parsed: ParsedPage | None, *, any_page: bool = False) -> str | None:
    """Why a form page read cannot be taken as `page`'s form: None unless the page answered without a
    single form control (a "Please wait" document, a truncated body). Such an answer is a failed read:
    reading it as a form with no fields would publish an empty backup and make restore treat every
    dumped field as one the firmware dropped. `any_page` extends the check to every page (a write plan
    is built from the page's controls whichever page it targets); a button is a control there. The hidden
    `nonce` and `hashpassword` inputs every answer carries are session plumbing, not controls (compared
    case-insensitively, like the client's form gate): a page showing only them is control-less."""
    if parsed is None or (page not in FORM_PAGES and not any_page):
        return None
    data_fields = any(f.name.lower() not in ("nonce", "hashpassword") for f in parsed.fields)
    if data_fields or parsed.selects or parsed.textareas or (any_page and parsed.buttons):
        return None
    return f"the {page} page answered without any form controls; it is not read as an empty form"


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


def snapshot_read_pages(dump: Snapshot, selected_pages: Sequence[str] | None = None) -> tuple[str, ...]:
    """Shared read set for diff/restore/autorestore, preserving full-snapshot defaults.

    Each selected section currently carries its own dependencies: apphosting includes both the
    device-label/MAC mapping and service dropdown needed to compare and restore forwards. Keep
    that complete page, without requiring unrelated services, allocation, or metadata pages.
    Requested form pages absent from the dump have nothing to compare and need no live read.
    """
    if selected_pages is None:
        return snapshot_pages_for(page for page in OPTIONAL_FORM_PAGES if page in dump.forms)
    captured = {"services", "apphosting", "ipalloc", *dump.forms}
    required = captured.intersection(selected_pages)
    return tuple(page for page in SNAPSHOT_PAGES if page in required)


def truncated_page_problem(page: str, parsed: ParsedPage | None) -> str | None:
    """Why a page cannot be used for a write plan or a snapshot: the parser stopped at one of its
    bounds, so later fields, rows or buttons are missing and a plan or backup built from it would
    be partial (a diff would read the missing rows as removed)."""
    if parsed is None or not parsed.truncated:
        return None
    return f"the {page} page is larger than the parser's bounds and was only partly read; it is not used"


def extract_snapshot(
    pages: Mapping[str, ParsedPage],
    *,
    ts: str,
    router_host: str,
    all_clients: bool = False,
    include: Iterable[str] = (),
    selected_pages: Sequence[str] | None = None,
) -> Snapshot:
    # A collector may supply extra pages. Project before extracting anything: an unrelated
    # malformed table must not block selected sections, and unrequested metadata stays unknown.
    # Preserve the full ParsedPage for each selection, including forward identity dropdowns.
    if selected_pages is not None:
        pages = {page: parsed for page, parsed in pages.items() if page in selected_pages}
    for page, parsed in pages.items():
        problem = truncated_page_problem(page, parsed)
        if problem is not None:
            raise SnapshotExtractionError(problem)
    included = set(include)
    captured_forms = [page for page in FORM_PAGES if page in CORE_FORM_PAGES or page in included]
    forms: dict[str, dict[str, str]] = {}
    form_secrets: dict[str, list[str]] = {}
    form_unchecked_text: dict[str, list[str]] = {}
    live_form_evidence: dict[str, LiveFormEvidence] = {}
    for page in captured_forms:
        parsed = pages.get(page)
        if parsed is not None:
            forms[page] = _form_values(parsed)
            literal = _unchecked_text_fields(parsed, forms[page])
            if literal:
                form_unchecked_text[page] = literal
            live_form_evidence[page] = _live_form_evidence(parsed, forms[page])
            secrets = sorted({f.name for f in parsed.fields if f.type.lower() == "password"} & forms[page].keys())
            if secrets:
                form_secrets[page] = secrets
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
        form_secrets=form_secrets,
        form_unchecked_text=form_unchecked_text,
        live_form_evidence=live_form_evidence,
    )


def duplicate_service_names(services: Sequence[SnapshotService]) -> list[str]:
    """Service names (stripped, case-folded) that appear more than once, in first-seen order. The
    gateway keys services by name, so a dump holding two of one name is refused by the dump loader."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for service in services:
        name = service.name.strip().casefold()
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    return duplicates


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
        headers = _safe(", ".join(row.keys()))
        raise SnapshotExtractionError(f"{page}: column {field_name} not found; headers were [{headers}]")
    return key


# parse_page flattens every >=3-column table on a page into one row list, so rows without the
# identifying column are tolerated — but if the page has rows and NONE of them carry it, the
# header was renamed (firmware update) and silently returning [] would understate the router's
# state to both diff and restore --prune.
def _require_recognized_rows(page: str, rows: Sequence[Mapping[str, str]], recognized: int, fields: str) -> None:
    if rows and recognized == 0:
        raise SnapshotExtractionError(
            f"{page}: none of {len(rows)} table rows carry {fields} column(s); "
            f"headers were [{_safe(', '.join(rows[0].keys()))}]"
        )


def _safe(value: str) -> str:
    """Router text inside an error message: one terminal-safe line."""
    return sanitize_terminal_text(value, single_line=True)


_LEADING_INT = re.compile(r"^[+-]?\d+")


def _parse_port(value: str, page: str, field_name: str) -> int:
    # Mirrors Number.parseInt(value.trim(), 10): leading integer digits, anything else is NaN.
    match = _LEADING_INT.match(value.strip())
    if match is None:
        raise SnapshotExtractionError(f"{page}: {field_name} '{_safe(value)}' is not a port number")
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
            raise SnapshotExtractionError(f"ipalloc: fixed allocation row has an invalid MAC ('{_safe(mac)}')")
        if not IPV4_PATTERN.match(ip):
            raise SnapshotExtractionError(
                f"ipalloc: fixed allocation row for {_safe(mac)} has no IPv4 address in the name column ('{_safe(ip)}')"
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
                f"apphosting: device label '{_safe(device_label)}' is ambiguous ({len(macs)} devices); "
                "cannot dump forwards safely"
            )
        if not macs:
            raise SnapshotExtractionError(
                f"apphosting: device label '{_safe(device_label)}' (service '{_safe(service)}') is not in the "
                "router's device list; reconnect the device or delete that forward in the gateway UI, then re-run dump"
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


def canonical_form_value(
    value: str | None, name: str, live: Mapping[str, str] | None, evidence: LiveFormEvidence | None
) -> str | None:
    """Interpret old checked-default empties only when live HTML rules out a real empty choice."""
    if value == "" and live is not None and name in live and evidence and name in evidence.legacy_default_names:
        return "on"
    return value


def legacy_form_spelling(dump: str | None, live: str | None, name: str, evidence: LiveFormEvidence | None) -> bool:
    """True when `dump` is how an earlier parser spelled the unchanged `live` value.

    Before form data was kept raw, every attribute value (input and option values) had U+00A0
    replaced by a plain space, and textarea text was whitespace-normalised (collapsed and trimmed).
    A dump taken then would otherwise diff against the unchanged router forever, and restore would
    post the altered spelling back. Whitespace collapse is forgiven only for live textareas.
    """
    if dump is None or live is None or dump == live:
        return False
    if dump == live.replace("\xa0", " "):
        return True
    return evidence is not None and name in evidence.textarea_names and dump == normalize_whitespace(live)


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
                    f"{parsed.page}: control '{_safe(f.name)}' has submit value '{UNCHECKED}', which collides with the "
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


def _unchecked_text_fields(parsed: ParsedPage, values: Mapping[str, str]) -> list[str]:
    """Names in `values` that hold the UNCHECKED string as a real text/select/textarea value rather
    than as the off marker of a checkbox/radio control."""
    other = {f.name for f in parsed.fields if f.type not in ("checkbox", "radio")}
    other.update(s.name for s in parsed.selects)
    other.update(t.name for t in parsed.textareas)
    return sorted(n for n, v in values.items() if v == UNCHECKED and n in other)


def _live_form_evidence(parsed: ParsedPage, values: Mapping[str, str]) -> LiveFormEvidence:
    # Before the parser implemented HTML default/on mode, a checked control without a
    # value attribute was saved as "". Only live evidence can disambiguate that legacy
    # spelling. An explicit empty radio option (or same-name text/hidden/select) must
    # retain its real empty value, even when an implicit-default sibling also exists.
    candidates = {f.name for f in parsed.fields
                  if f._value_omitted and f.type in ("checkbox", "radio") and f.value == "on" and not f.disabled}
    ambiguous = {f.name for f in parsed.fields if f.type not in ("checkbox", "radio") or f.value == ""}
    ambiguous.update(s.name for s in parsed.selects)
    ambiguous.update(t.name for t in parsed.textareas)
    disabled = {f.name for f in parsed.fields if f.disabled}
    disabled.update(s.name for s in parsed.selects if s.disabled)
    disabled.update(t.name for t in parsed.textareas if t.disabled)
    return LiveFormEvidence(
        legacy_default_names=frozenset((candidates - ambiguous) & values.keys()),
        disabled_names=frozenset(disabled - values.keys()),
        textarea_names=frozenset(t.name for t in parsed.textareas),
        rendered_names=frozenset(
            {f.name for f in parsed.fields} | {s.name for s in parsed.selects} | {t.name for t in parsed.textareas}
        ),
    )


def _firmware_version(parsed: ParsedPage | None) -> str:
    if parsed is None:
        return ""
    for key, value in parsed.values.items():
        if _normalize(key) in ("softwareversion", "firmwareversion"):
            return value.strip()
    return ""
