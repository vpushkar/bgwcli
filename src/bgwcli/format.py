"""Human-readable renderers and --json-facing dict builders. Port of BGW320-CLI src/format.ts plus the
inline printers that lived in src/cli.ts (tabs, actions, session status, coverage, status sections).

Conventions:
  - every printer writes to `stream` (default sys.stdout) so tests capture through io.StringIO;
  - every string that came from the router passes through sanitize_terminal_text();
  - --json builders return plain dicts with the TS camelCase keys (types.to_json_dict) and redact
    secrets via redact.redact_value unless include_secrets;
  - embedded JSON in text output (payloads, changes) is compact like JSON.stringify.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from typing import Any, TextIO, TypeVar

from .actions import RouterAction
from .audit import AuditResult
from .devices import DeviceListResult
from .fetch import ParsedPageResult
from .mutations import DANGEROUS_PAGES, MutationPlan, confirm_token_for_page
from .operations import OperationResult, set_dry_run
from .pages import RouterTab
from .redact import redact_value
from .restore import RestoreExecution, RestoreStep, RestoreStepResult, follow_up_payload
from .session import SessionState
from .snapshot import Snapshot, SnapshotService
from .snapshot_diff import FormFieldDiff, ReservationChange, SnapshotDiff
from .status import StatusResult, StatusSection
from .sweep import SweepPage
from .types import Device, LogEntry, ParsedField, ParsedPage, ParsedSelect, SitemapEntry, _camel, to_json_dict

T = TypeVar("T")
DEFAULT_LIMIT = 20
_MAX_KEY_WIDTH = 42
_MAX_CELL_WIDTH = 36
_HIDDEN_FIELDS = frozenset({"nonce", "hashpassword"})

_OSC = re.compile("\x1b\\][^\x07]*(?:\x07|\x1b\\\\)")
_CSI = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]")
_WHITESPACE_RUN = re.compile(r"[\n\t]+")


# ---------------------------------------------------------------------------------------------
# primitives


def sanitize_terminal_text(value: str, *, single_line: bool = False) -> str:
    """Strip OSC/CSI escape sequences and control characters (keeping \\n and \\t) from router text."""
    sanitized = _CSI.sub("", _OSC.sub("", value))
    sanitized = "".join(
        ch for ch in sanitized if ch in "\n\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159)
    )
    return _WHITESPACE_RUN.sub(" ", sanitized) if single_line else sanitized


def _line(text: str) -> str:
    return sanitize_terminal_text(text, single_line=True)


def _compact_json(value: Any) -> str:
    """JSON.stringify(value) equivalent: compact separators, unicode kept as-is."""
    return json.dumps(to_json_dict(value), separators=(",", ":"), ensure_ascii=False)


def print_json(value: Any, stream: TextIO | None = None) -> None:
    """Pretty JSON (2-space indent) for --json output; dataclasses go through to_json_dict."""
    (stream or sys.stdout).write(json.dumps(to_json_dict(value), indent=2, ensure_ascii=False) + "\n")


def json_with_nulls(value: Any, null_keys: Iterable[str]) -> Any:
    """to_json_dict() that keeps `None` for the given snake_case field names (emitted as JSON null).

    The TS CLI leaves most absent fields undefined (dropped) but sets a few to `null` explicitly,
    e.g. OperationResult.location / RestoreStepResult.location after a committed POST without a
    Location header. Use this where parity with that output matters.
    """
    keep = frozenset(null_keys)

    def convert(item: Any) -> Any:
        if is_dataclass(item) and not isinstance(item, type):
            out: dict[str, Any] = {}
            for f in fields(item):
                v = getattr(item, f.name)
                if v is None and f.name not in keep:
                    continue
                out[_camel(f.name)] = convert(v)
            return out
        if isinstance(item, dict):
            return {k: convert(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [convert(v) for v in item]
        return item

    return convert(value)


def print_key_values(values: Mapping[str, str], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if not values:
        return
    width = min(_MAX_KEY_WIDTH, max(12, *(len(key) for key in values)))
    for key, value in values.items():
        out.write(f"{_line(key).ljust(width)}  {_line(value)}\n")


def print_rows(rows: Sequence[Mapping[str, str]], columns: Sequence[str], stream: TextIO | None = None) -> None:
    """Generic table: header, dashed rule, rows; cells clipped to 36 chars with an ellipsis."""
    out = stream or sys.stdout
    if not rows:
        out.write("(none)\n")
        return
    widths = [
        min(_MAX_CELL_WIDTH, max(len(column), *(len(str(row.get(column, "") or "")) for row in rows)))
        for column in columns
    ]
    out.write("  ".join(_line(column).ljust(width) for column, width in zip(columns, widths, strict=True)) + "\n")
    out.write("  ".join("-" * width for width in widths) + "\n")
    for row in rows:
        cells = (
            _truncate(_line(str(row.get(column, "") or "")), width).ljust(width)
            for column, width in zip(columns, widths, strict=True)
        )
        out.write("  ".join(cells) + "\n")


def _truncate(value: str, width: int) -> str:
    return value[: max(0, width - 1)] + "…" if len(value) > width else value


def _print_overflow(total: int, limit: int, out: TextIO) -> None:
    if total > limit:
        out.write(f"... {total - limit} more rows. Use --limit {total} to show all.\n")


def _table_keys(rows: Iterable[Mapping[str, str]]) -> list[str]:
    return list(dict.fromkeys(key for row in rows for key in row))


def _count_by(rows: Iterable[Mapping[str, str]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key) or "(blank)"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _format_counts(counts: Mapping[str, int]) -> str:
    ordered = sorted(counts.items(), key=lambda item: -item[1])  # stable: ties keep insertion order
    return ", ".join(f"{key}: {count}" for key, count in ordered) if ordered else "(none)"


def _compact(values: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value}


# ---------------------------------------------------------------------------------------------
# listings: sitemap / tabs / actions / coverage / session / check


def print_sitemap(entries: Sequence[SitemapEntry], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    page_width = max(4, *(len(entry.page) for entry in entries)) if entries else 4
    for entry in entries:
        out.write(f"{_line(entry.page).ljust(page_width)}  {_line(entry.label)}\n")


def print_tabs(tabs: Sequence[RouterTab], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    section_width = max(7, *(len(tab.section) for tab in tabs)) if tabs else 7
    label_width = max(5, *(len(tab.label) for tab in tabs)) if tabs else 5
    for tab in tabs:
        danger = "  guarded" if tab.dangerous else ""
        out.write(f"{tab.section.ljust(section_width)}  {tab.label.ljust(label_width)}  {tab.page}{danger}\n")


def print_actions(actions: Sequence[RouterAction], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    for action in actions:
        guard = "dangerous" if action.dangerous else "guarded"
        out.write(
            f"{action.name.ljust(30)} {action.page.ljust(12)} {guard.ljust(9)} "
            f"confirm={action.confirm_token.ljust(21)} {action.description}\n"
        )


def coverage_output(*, mapped_pages: Iterable[str], live_pages: Iterable[str]) -> dict[str, Any]:
    mapped = sorted(set(mapped_pages))
    live = sorted(set(live_pages))
    return {
        "mappedCount": len(mapped),
        "liveCount": len(live),
        "missingFromCli": [page for page in live if page not in mapped],
        "notInLiveSitemap": [page for page in mapped if page not in live],
    }


def print_coverage(coverage: Mapping[str, Any], stream: TextIO | None = None) -> None:
    print_key_values({
        "Mapped pages": str(coverage["mappedCount"]),
        "Live sitemap pages": str(coverage["liveCount"]),
        "Missing from CLI": ", ".join(coverage["missingFromCli"]) or "(none)",
        "Not in live sitemap": ", ".join(coverage["notInLiveSitemap"]) or "(none)",
    }, stream)


def print_session_state(state: SessionState, stream: TextIO | None = None) -> None:
    print_key_values({
        "Cached session": "yes" if state.cached else "no",
        "Cache expires": _iso_ms(state.cache_expires_at),
        "Pool cooldown until": _iso_ms(state.pool_cooldown_until),
    }, stream)


def _iso_ms(epoch_ms: int | None) -> str:
    """new Date(ms).toISOString() equivalent; '(none)' for a missing timestamp."""
    if not epoch_ms:
        return "(none)"
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def print_check_result(host: str, reachable: bool, title: str, stream: TextIO | None = None) -> None:
    print_key_values({"Host": host, "Reachable": "yes" if reachable else "no", "Title": title}, stream)


# ---------------------------------------------------------------------------------------------
# devices / logs / sweep / audit / fetch errors


def _summarize_connection(value: str) -> str:
    return re.sub(r"\s+Type:.*$", "", value, flags=re.IGNORECASE).strip()


def _print_devices(devices: Sequence[Device], limit: int, out: TextIO) -> None:
    if devices:
        by_status = _count_by([{"Status": device.status or "(blank)"} for device in devices], "Status")
        by_connection = _count_by(
            [{"Connection": _summarize_connection(device.connection) or "(blank)"} for device in devices], "Connection"
        )
        print_key_values({
            "Total devices": str(len(devices)),
            "By status": _format_counts(by_status),
            "By connection": _format_counts(by_connection),
        }, out)
        out.write("\n")

    columns = ["Status", "IP", "MAC", "Connection", "Speed", "Last activity", "Name"]
    print_rows([{
        "Status": device.status,
        "IP": device.ip,
        "MAC": device.mac,
        "Connection": _summarize_connection(device.connection),
        "Speed": device.connection_speed or "",
        "Last activity": device.last_activity or "",
        "Name": device.name,
    } for device in devices[:limit]], columns, out)
    _print_overflow(len(devices), limit, out)


def print_device_list(result: DeviceListResult, *, limit: int = DEFAULT_LIMIT, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if result.fallback:
        out.write("Device List (fallback from IP Allocation)\n")
        if result.error:
            out.write(f"devices.ha did not return: {_line(result.error)}\n")
        out.write("\n")
    _print_devices(result.devices, limit, out)


def print_logs(logs: Sequence[LogEntry], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if not logs:
        out.write("No log entries found.\n")
        return
    by_reason = _count_by([{"Reason": entry.reason or "(blank)"} for entry in logs], "Reason")
    by_protocol = _count_by([{"Protocol": entry.protocol or "(blank)"} for entry in logs], "Protocol")
    print_key_values({
        "Entries": str(len(logs)),
        "By reason": _format_counts(by_reason),
        "By protocol": _format_counts(by_protocol),
    }, out)
    out.write("\n")
    print_rows([{
        "Time": entry.time,
        "Source": entry.source,
        "Destination": entry.destination,
        "Proto": entry.protocol,
        "Reason": entry.reason,
    } for entry in logs], ["Time", "Source", "Destination", "Proto", "Reason"], out)


_SCAN_COLUMNS = [
    "Section", "Tab", "Page", "Status", "Fallback", "Title", "Values", "Entries", "Tables", "Fields",
    "Selects", "Buttons", "Forms", "Links", "Data", "Guarded",
]


def print_scans(scans: Sequence[SweepPage], stream: TextIO | None = None) -> None:
    """Per-page line for sweep / scan / schema."""
    print_rows([{
        "Section": scan.section,
        "Tab": scan.label,
        "Page": scan.page,
        "Status": str(scan.status_code if scan.status_code is not None else "") if scan.ok else "error",
        "Fallback": "yes" if scan.fallback else "",
        "Title": scan.title or scan.error or "",
        "Values": str(scan.value_count or 0),
        "Entries": str(scan.value_entry_count or 0),
        "Tables": str(scan.table_rows or 0),
        "Fields": str(scan.field_count or 0),
        "Selects": str(scan.select_count or 0),
        "Buttons": str(scan.button_count or 0),
        "Forms": str(scan.form_count or 0),
        "Links": str(scan.link_count or 0),
        "Data": str(scan.data_count or 0),
        "Guarded": "yes" if scan.dangerous else "",
    } for scan in scans], _SCAN_COLUMNS, stream)


def print_audit(audit: AuditResult, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    print_key_values({
        "Total pages": str(audit.total_pages),
        "OK pages": str(audit.ok_pages),
        "Failed pages": str(audit.failed_pages),
        "Fallback pages": str(audit.fallback_pages),
        "Useful pages": str(audit.useful_pages),
        "Empty OK pages": str(audit.empty_pages),
        "Guarded pages": str(audit.dangerous_pages),
    }, out)

    interesting = [page for page in audit.pages if not page.ok or page.fallback or not page.useful]
    if interesting:
        out.write("\nNeeds attention\n")
        print_rows([{
            "Section": page.section,
            "Tab": page.label,
            "Page": page.page,
            "Status": "ok" if page.ok else "error",
            "Fallback": "yes" if page.fallback else "",
            "Data": str(page.data_count or 0),
            "Detail": page.error or page.title or "",
        } for page in interesting], ["Section", "Tab", "Page", "Status", "Fallback", "Data", "Detail"], out)


def print_page_fetch_error(result: ParsedPageResult, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write(f"Page unavailable: {result.page}\n")
    if result.error:
        out.write(f"{sanitize_terminal_text(result.error)}\n")


def summarize_fetch_failures(failures: Iterable[ParsedPageResult]) -> list[dict[str, Any]]:
    """JSON view of failed fetches without the parsed page (snapshot pages are parsed with secrets)."""
    return [to_json_dict(replace(failure, parsed=None)) for failure in failures]


# ---------------------------------------------------------------------------------------------
# operations / mutation plans


def print_operation(result: OperationResult, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if result.dry_run:
        out.write(f"dry-run: no router {result.operation} was sent\n")
    else:
        out.write(f"{result.operation} committed\n")

    values: dict[str, str] = {
        "Operation": result.operation,
        "Page": result.page,
        "Guarded": "yes" if result.guarded else "no",
        "Dangerous": "yes" if result.dangerous else "no",
    }
    if result.action:
        values["Action"] = result.action
    if result.button:
        values["Button"] = result.button
    if result.target:
        values["Target"] = result.target
    if result.confirmation:
        values["Confirmation"] = result.confirmation
    if result.commit_command:
        values["Commit command"] = result.commit_command
    if result.status_code is not None:
        values["Status"] = str(result.status_code)
    if result.location:
        values["Location"] = result.location
    if result.payload is not None:
        values["Payload"] = _compact_json(result.payload)
    if result.changes is not None:
        values["Changes"] = _compact_json(result.changes)
    if result.verified is not None:
        values["Verified"] = "yes" if result.verified else "no"
    if result.warning:
        values["Warning"] = result.warning
    print_key_values(values, out)
    if result.mismatches:
        out.write("\nNot applied (live value differs from the requested one)\n")
        for name, pair in result.mismatches.items():
            wanted = sanitize_terminal_text(pair.get("wanted", ""))
            live = sanitize_terminal_text(pair.get("live", "<absent>"))
            out.write(f"{sanitize_terminal_text(name)}: wanted {wanted!r}, live {live!r}\n")
    if result.result:
        out.write("\nResult\n")
        out.write(f"{sanitize_terminal_text(result.result)}\n")


def print_mutation_plan(plan: MutationPlan, *, confirmation: str | None = None, stream: TextIO | None = None) -> None:
    """Dry-run view of a `set` plan: redacted payload, changes and the commit command."""
    print_operation(set_dry_run(plan, confirmation or confirm_token_for_page(plan.page)), stream)


# TS builders that take `location: string | null` and always assign it (operations.ts actionCommitted /
# setCommitted / submitCommitted). diagnosticCommitted and restoreCommitted never set the key.
_OPERATIONS_WITH_LOCATION = frozenset({"action", "set", "submit"})


def operation_output(result: OperationResult) -> dict[str, Any]:
    """--json dict for an OperationResult. Committed action/set/submit POSTs carry an explicit
    `"location": null` like TS; every other operation drops the key when absent."""
    if result.committed and result.status_code is not None and result.operation in _OPERATIONS_WITH_LOCATION:
        return json_with_nulls(result, {"location"})
    return to_json_dict(result)


def execution_output(execution: RestoreExecution) -> dict[str, Any]:
    """--json dict for a RestoreExecution mirroring restore.ts executeRestore.

    Only step results built from a POST response (`applied` / `failed` with a statusCode) assign
    `location` (string or null); thrown/blocked/skipped/not-run steps never carry the key. Non-null
    locations (confirmWarningPage failures) survive to_json_dict on their own.
    """
    out = to_json_dict(execution)
    out["steps"] = [
        json_with_nulls(step, {"location"})
        if step.status in ("applied", "failed") and step.status_code is not None
        else to_json_dict(step)
        for step in execution.steps
    ]
    return out


# ---------------------------------------------------------------------------------------------
# status views


def print_status_sections(sections: Sequence[StatusSection], stream: TextIO | None = None) -> None:
    """`bgw status`: one block per section, blank line before each title."""
    out = stream or sys.stdout
    for section in sections:
        out.write(f"\n{_line(section.title or section.heading or section.page)}\n")
        if not section.ok:
            out.write(f"{sanitize_terminal_text(section.error or 'unavailable')}\n")
            continue
        print_key_values(section.values, out)


def print_device_status(result: StatusResult, *, limit: int = DEFAULT_LIMIT, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if not result.fallback and result.parsed is not None:
        print_parsed_page(result.parsed, limit=limit, stream=out)
        return
    out.write("Device Status (fallback summary)\n\n")
    if result.error:
        out.write(f"home.ha did not return: {_line(result.error)}\n\n")
    _print_device_status_summary(result.sections, out)


def print_composite_status(
    title: str, result: StatusResult, *, limit: int = DEFAULT_LIMIT, stream: TextIO | None = None
) -> None:
    out = stream or sys.stdout
    if not result.fallback and result.parsed is not None:
        print_parsed_page(result.parsed, limit=limit, stream=out)
        return
    out.write(f"{title} (fallback)\n\n")
    if result.error:
        out.write(f"{_line(result.page)}.ha did not return: {_line(result.error)}\n\n")
    _print_fallback_sections(result.sections, limit, out)


def _print_fallback_sections(sections: Sequence[StatusSection], limit: int, out: TextIO) -> None:
    for section in sections:
        out.write(f"{_line(section.title or section.heading or section.page)}\n")
        if not section.ok:
            out.write(f"{sanitize_terminal_text(section.error or 'unavailable')}\n\n")
            continue
        print_key_values(section.values, out)
        if section.tables:
            out.write("\n")
            print_rows(section.tables[:limit], _table_keys(section.tables), out)
            _print_overflow(len(section.tables), limit, out)
        out.write("\n")


def _print_device_status_summary(sections: Sequence[StatusSection], out: TextIO) -> None:
    for section in sections:
        out.write(f"{_line(section.title or section.heading or section.page)}\n")
        if not section.ok:
            out.write(f"{sanitize_terminal_text(section.error or 'unavailable')}\n\n")
            continue
        print_key_values(_summarize_status_section(section), out)
        out.write("\n")


_SYSINFO_KEYS = ["Manufacturer", "Model Number", "Software Version", "Time Since Last Reboot", "Current Date/Time"]
_BROADBAND_KEYS = [
    "Broadband Connection", "Broadband IPv4 Address", "Gateway IPv4 Address", "Current Speed (Mbps)",
    "Line State", "PON Link Status", "UNI Status",
]


def _summarize_status_section(section: StatusSection) -> dict[str, str]:
    if section.page == "sysinfo":
        return _select_keys(section.values, _SYSINFO_KEYS)
    if section.page == "broadbandstatistics":
        return _select_keys(section.values, _BROADBAND_KEYS)
    return section.values


def _select_keys(values: Mapping[str, str], keys: Iterable[str]) -> dict[str, str]:
    return {key: values[key] for key in keys if values.get(key)}


# ---------------------------------------------------------------------------------------------
# parsed pages


def parsed_page_output(page: ParsedPage) -> dict[str, Any]:
    """--json shape for a page: the camelCase ParsedPage plus a scriptable `summary`."""
    out = to_json_dict(page)
    out["summary"] = summarize_parsed_page(page)
    return out


def print_parsed_page(
    page: ParsedPage, *, forms: bool = False, limit: int = DEFAULT_LIMIT, stream: TextIO | None = None
) -> None:
    out = stream or sys.stdout
    out.write(f"{_line(page.title or page.page)}\n\n")

    if _print_specialized_page(page, limit, out):
        if forms:
            _print_form_details(page, out)
        return

    print_key_values(page.values, out)

    if page.tables:
        out.write("\nTables\n")
        print_rows(page.tables[:limit], _table_keys(page.tables), out)
        _print_overflow(len(page.tables), limit, out)

    if page.buttons and not forms:
        out.write("\nAvailable actions\n")
        print_rows(_button_rows(page.buttons), ["Name", "Label", "Type"], out)

    if not forms and (page.fields or page.selects or page.textareas):
        _print_controls(page, out)

    if forms:
        _print_form_details(page, out)


def _button_rows(buttons: Iterable[Any]) -> list[dict[str, str]]:
    return [{"Name": button.name, "Label": button.label, "Type": button.type} for button in buttons]


def _print_controls(page: ParsedPage, out: TextIO) -> None:
    out.write("\nControls\n")
    print_key_values({
        "Fields": str(sum(1 for f in page.fields if f.name not in _HIDDEN_FIELDS)),
        "Selects": str(len(page.selects)),
        "Textareas": str(len(page.textareas)),
        "Use --forms": "show controls and submit targets",
    }, out)


def _print_actions_and_controls(page: ParsedPage, limit: int, out: TextIO) -> None:
    if page.buttons:
        out.write("\nAvailable actions\n")
        print_rows(_button_rows(page.buttons[:limit]), ["Name", "Label", "Type"], out)
        _print_overflow(len(page.buttons), limit, out)
    if page.fields or page.selects or page.textareas:
        _print_controls(page, out)


# Page -> (preferred field names, preferred select names) for the form-state view.
_FORM_STATE_PAGES: dict[str, tuple[list[str], list[str]]] = {
    "packetfilter": ([], ["filter_enable", "packet_filter", "protocol"]),
    "etherlan": (["ipaddr", "ipmask"], ["lanipv6"]),
    "ip6lan": (["ip6addr", "ip6prefixlen"], ["ipv6_enable", "dhcp6s_enable", "radvd_enable"]),
    "wmacauth": (["macaddr", "hostname"], ["filtering", "ssid"]),
    "remoteaccess": (["port"], ["remote_enable"]),
    "routerpasswd": ([], []),
    "pshosts": (["pubhost"], ["device"]),
    "services": (["name", "globalportstart", "globalportend", "basehostport"], ["protocol"]),
    "events": ([], []),
}

_CONFIG_PAGES: dict[str, list[str]] = {
    "firewall": ["Packet Filter", "IP Passthrough", "NAT Default Server", "Firewall Advanced"],
    "syslog": ["Syslog", "Server IP Address", "Server Port", "Log Level"],
}

_ACTION_ONLY_PAGES = frozenset({"restart", "update", "reset"})
_VOICE_PAGES = frozenset({"voice", "voiceconfig", "voicestat"})

_SUMMARY_BUILDERS: dict[str, Callable[[ParsedPage], dict[str, str]]] = {}


def summarize_parsed_page(page: ParsedPage) -> dict[str, str]:
    """Scriptable per-page summary (the `summary` key of --json page output)."""
    builder = _SUMMARY_BUILDERS.get(page.page)
    if builder is not None:
        return builder(page)
    if page.page in _FORM_STATE_PAGES:
        # pshosts/services print with different preferred names than they summarize with (TS parity).
        preferred = {"pshosts": ([], []), "services": ([], [])}.get(page.page, _FORM_STATE_PAGES[page.page])
        return _form_state_values(page, *preferred)
    return page.values


def _ipalloc_summary(page: ParsedPage) -> dict[str, str]:
    return {
        "Total entries": str(len(page.tables)),
        "By allocation": _format_counts(_count_by(page.tables, "Allocation")),
        "By status": _format_counts(_count_by(page.tables, "Status")),
    }


def _nattable_summary(page: ParsedPage) -> dict[str, str]:
    return _compact({
        **_select_keys(page.values, ["Total sessions available", "Total sessions in use", "Select display option"]),
        "Displayed sessions": str(len(page.tables)) if page.tables else None,
        "By protocol": _format_counts(_count_by(page.tables, "Protocol")) if page.tables else None,
    })


def _speed_summary(page: ParsedPage) -> dict[str, str]:
    if not page.tables:
        return page.values
    downstream = next((row for row in page.tables if re.search("downstream", row.get("Direction", ""), re.I)), None)
    upstream = next((row for row in page.tables if re.search("upstream", row.get("Direction", ""), re.I)), None)
    return _compact({
        "Results": str(len(page.tables)),
        "By result": _format_counts(_count_by(page.tables, "Result")),
        "Latest downstream Mbps": downstream.get("Mbps") if downstream else None,
        "Latest upstream Mbps": upstream.get("Mbps") if upstream else None,
    })


def _broadband_config_summary(page: ParsedPage) -> dict[str, str]:
    return _compact({
        "Broadband source": _select_value(page, "source"),
        "Base MTU": _field_value(page, "MTUW"),
        "IPv6 MTU": _field_value(page, "MTU6"),
    })


def _wifi_summary(page: ParsedPage) -> dict[str, str]:
    octet = _field_value(page, "u_octet")
    return _compact({
        "Home SSID": _field_value(page, "home_ssidname"),
        "Home SSID enabled": _on_off(_select_value(page, "u_ussidenable")),
        "Home security": _select_value(page, "homeSSID_security"),
        "Home password": _field_value(page, "homeSSID_key"),
        "Guest SSID": _field_value(page, "guest_ssidname"),
        "Guest SSID enabled": _on_off(_select_value(page, "u_gssidenable")),
        "Guest access": _guest_access(_select_value(page, "u_gssidisolate")),
        "Guest subnet": f"192.168.{octet}.0/24" if octet else "",
    })


def _advanced_wifi_summary(page: ParsedPage) -> dict[str, str]:
    radios = [row for row in page.tables if row.get("Section") == "Radio"]
    ssids = [row for row in page.tables if row.get("Section") == "SSID"]
    return _compact({
        "Radio configurations": str(len(radios)) if radios else None,
        "SSID configurations": str(len(ssids)) if ssids else None,
        "2.4 GHz enabled": _select_value(page, "wl80211on"),
        "2.4 GHz standard": _select_value(page, "standard"),
        "2.4 GHz bandwidth": _select_value(page, "bandwidth"),
        "2.4 GHz channel": _select_value(page, "channelplusauto"),
        "5 GHz enabled": _select_value(page, "wl80211on_5"),
        "5 GHz standard": _select_value(page, "standard_5"),
        "5 GHz bandwidth": _select_value(page, "bandwidth_5"),
    })


def _dhcp_server_summary(page: ParsedPage) -> dict[str, str]:
    return _compact({
        "Gateway address": _field_value(page, "ipaddr"),
        "Subnet mask": _field_value(page, "ipmask"),
        "DHCP enabled": _on_off(_select_value(page, "dhcp")),
        "DHCP start": _field_value(page, "dhcpstart"),
        "DHCP end": _field_value(page, "dhcpend"),
        "DHCP lease": _duration_fields(page, "dhcp"),
        "Primary pool": _checked_radio_value(page, "primpool"),
        "Public subnet": _on_off(_select_value(page, "pubsub")),
        "Allow inbound": _on_off(_select_value(page, "ain")),
        "Cascaded router": _on_off(_select_value(page, "cr")),
    })


def _ip_passthrough_summary(page: ParsedPage) -> dict[str, str]:
    return _compact({
        "Allocation mode": _select_value(page, "allocmode"),
        "Default server": _field_value(page, "defsrvint"),
        "Passthrough mode": _select_value(page, "passmode"),
        "Passthrough MAC": _field_value(page, "passmac"),
        "DHCP lease": _duration_fields(page, "dhcp"),
    })


def _app_hosting_summary(page: ParsedPage) -> dict[str, str]:
    service = _select_by_name(page, "service")
    device = _select_by_name(page, "device")
    return _compact({
        "Selected service": service.value if service else "",
        "Selected device": device.value if device else "",
        "Known services": str(len(service.options)) if service else "",
        "Known devices": str(len(device.options)) if device else "",
    })


def _firewall_advanced_summary(page: ParsedPage) -> dict[str, str]:
    return _compact({
        "Drop ICMP to LAN": _on_off(_select_value(page, "downstream_echo_rqst_drop")),
        "Drop ICMP to device LAN": _on_off(_select_value(page, "downstream_echo_rqst_drop_lan")),
        "Drop ICMP to device WAN": _on_off(_select_value(page, "icmp_downstream_echo_rqst_drop_wan")),
        "Reflexive ACL": _on_off(_select_value(page, "reflexive")),
        "ESP ALG": _on_off(_select_value(page, "algesp")),
        "SIP ALG": _on_off(_select_value(page, "algsip")),
    })


_SUMMARY_BUILDERS.update({
    "ipalloc": _ipalloc_summary,
    "nattable": _nattable_summary,
    "speed": _speed_summary,
    "broadbandconfig": _broadband_config_summary,
    "wconfig_unified": _wifi_summary,
    "wconfig": _advanced_wifi_summary,
    "dhcpserver": _dhcp_server_summary,
    "ippass": _ip_passthrough_summary,
    "apphosting": _app_hosting_summary,
    "dosprotect": _firewall_advanced_summary,
})


def _print_specialized_page(page: ParsedPage, limit: int, out: TextIO) -> bool:
    """Page-specific human layout; returns False when the generic layout should be used."""
    name = page.page
    if name == "ipalloc":
        print_key_values(summarize_parsed_page(page), out)
        if page.tables:
            out.write("\nAllocations\n")
            columns = ["IPv4 Address / Name", "MAC Address", "Status", "Allocation", "Action"]
            print_rows(page.tables[:limit], columns, out)
            _print_overflow(len(page.tables), limit, out)
        _print_actions_and_controls(page, limit, out)
    elif name == "nattable":
        print_key_values(summarize_parsed_page(page), out)
        if page.tables:
            out.write("\nSessions\n")
            columns = [
                "Protocol", "TCP State", "Source Address", "Source Port", "Destination Address", "Destination Port",
            ]
            print_rows(page.tables[:limit], columns, out)
            _print_overflow(len(page.tables), limit, out)
        _print_actions_and_controls(page, limit, out)
    elif name == "diag":
        if page.tables:
            out.write("Diagnostics\n")
            print_rows(page.tables, list(page.tables[0]), out)
        _print_actions_and_controls(page, DEFAULT_LIMIT, out)
    elif name == "speed":
        if page.tables:
            print_key_values(summarize_parsed_page(page), out)
            out.write("\nHistory\n")
            print_rows(page.tables[:limit], ["Time", "Direction", "Mbps", "Latency ms", "Result"], out)
            _print_overflow(len(page.tables), limit, out)
        else:
            print_key_values(page.values, out)
        _print_actions_and_controls(page, limit, out)
    elif name == "wconfig_unified":
        print_key_values(_wifi_summary(page), out)
        if page.tables:
            out.write("\nCurrent radios\n")
            print_rows(page.tables, ["Radio", "Current Channel", "Channel Width", "Mode"], out)
        _print_actions_and_controls(page, DEFAULT_LIMIT, out)
    elif name == "wconfig":
        print_key_values(_advanced_wifi_summary(page), out)
        if page.tables:
            out.write("\nRadio and SSID configuration\n")
            print_rows(page.tables[:limit], _table_keys(page.tables), out)
            _print_overflow(len(page.tables), limit, out)
        _print_actions_and_controls(page, limit, out)
    elif name in ("broadbandconfig", "dhcpserver", "ippass", "apphosting", "dosprotect"):
        print_key_values(_SUMMARY_BUILDERS[name](page), out)
        _print_actions_and_controls(page, DEFAULT_LIMIT, out)
    elif name in _FORM_STATE_PAGES:
        _print_form_state_page(page, *_FORM_STATE_PAGES[name], out=out)
    elif name in _CONFIG_PAGES:
        values = _select_keys(page.values, _CONFIG_PAGES[name])
        if values:
            print_key_values(values, out)
        _print_actions_and_controls(page, DEFAULT_LIMIT, out)
    elif name in _ACTION_ONLY_PAGES:
        if page.values:
            print_key_values(page.values, out)
        _print_tables_block(page, DEFAULT_LIMIT, out)
        _print_actions_and_controls(page, DEFAULT_LIMIT, out)
    elif name in _VOICE_PAGES:
        _print_voice_page(page, limit, out)
    elif name == "logs":
        out.write("No log entries found.\n")
    else:
        return False
    return True


def _print_tables_block(page: ParsedPage, limit: int, out: TextIO) -> None:
    if page.tables:
        out.write("\nTables\n")
        print_rows(page.tables[:limit], _table_keys(page.tables), out)
        _print_overflow(len(page.tables), limit, out)


def _print_form_state_page(
    page: ParsedPage, preferred_fields: Sequence[str], preferred_selects: Sequence[str], *, out: TextIO
) -> None:
    values = _form_state_values(page, preferred_fields, preferred_selects)
    if values:
        print_key_values(values, out)
    elif page.values:
        print_key_values(page.values, out)
    _print_tables_block(page, DEFAULT_LIMIT, out)
    _print_actions_and_controls(page, DEFAULT_LIMIT, out)


def _print_voice_page(page: ParsedPage, limit: int, out: TextIO) -> None:
    rows = page.tables
    if rows:
        sections = _count_by([row for row in rows if row.get("Section")], "Section")
        summary = {"Reported rows": str(len(rows))}
        if sections:
            summary["Sections"] = _format_counts(sections)
        print_key_values(summary, out)
        out.write("\n")
        print_rows(rows[:limit], _table_keys(rows), out)
        _print_overflow(len(rows), limit, out)
    else:
        print_key_values(page.values, out)
    _print_actions_and_controls(page, DEFAULT_LIMIT, out)


def _print_form_details(page: ParsedPage, out: TextIO) -> None:
    if page.buttons:
        out.write("\nCLI operations\n")
        token = confirm_token_for_page(page.page)
        rows = []
        for button in page.buttons:
            button_name = button.name or button.label
            commit = (
                "blocked on dangerous page"
                if page.page in DANGEROUS_PAGES
                else f"submit {page.page} {_quote_arg(button_name)} --commit --confirm {token}"
            )
            rows.append({
                "Button": button.label or button_name,
                "Dry run": f"submit {page.page} {_quote_arg(button_name)}",
                "Commit": commit,
            })
        print_rows(rows, ["Button", "Dry run", "Commit"], out)

    out.write("\nFields\n")
    print_rows([{
        "Name": f.name,
        "Type": f.type,
        "Value": f.value,
        "Checked": "yes" if f.checked else "",
        "Disabled": "yes" if f.disabled else "",
        "Readonly": "yes" if f.read_only else "",
        "Sensitive": "yes" if f.sensitive else "",
    } for f in page.fields], ["Name", "Type", "Value", "Checked", "Disabled", "Readonly", "Sensitive"], out)

    if page.selects:
        out.write("\nSelects\n")
        print_rows([{
            "Name": s.name,
            "Value": s.value,
            "Options": _select_options(s),
            "Disabled": "yes" if s.disabled else "",
            "Sensitive": "yes" if s.sensitive else "",
        } for s in page.selects], ["Name", "Value", "Options", "Disabled", "Sensitive"], out)

    if page.textareas:
        out.write("\nTextareas\n")
        print_rows([{
            "Name": t.name,
            "Value": t.value,
            "Disabled": "yes" if t.disabled else "",
            "Readonly": "yes" if t.read_only else "",
            "Sensitive": "yes" if t.sensitive else "",
        } for t in page.textareas], ["Name", "Value", "Disabled", "Readonly", "Sensitive"], out)

    if page.buttons:
        out.write("\nButtons\n")
        print_rows([{
            "Name": b.name,
            "Type": b.type,
            "Value": b.value,
            "Label": b.label,
            "Disabled": "yes" if b.disabled else "",
            "Sensitive": "yes" if b.sensitive else "",
        } for b in page.buttons], ["Name", "Type", "Value", "Label", "Disabled", "Sensitive"], out)

    if page.forms:
        out.write("\nForms\n")
        print_rows([{
            "Method": form.method,
            "Action": form.action,
            "Fields": ", ".join(form.field_names),
            "Selects": ", ".join(form.select_names),
            "Textareas": ", ".join(form.textarea_names),
            "Buttons": ", ".join(form.button_names),
        } for form in page.forms], ["Method", "Action", "Fields", "Selects", "Textareas", "Buttons"], out)

    if page.value_entries:
        out.write("\nRouter-reported values\n")
        print_rows([{
            "Section": entry.section,
            "Label": entry.label,
            "Value": entry.value,
            "Default": entry.default_value or "",
        } for entry in page.value_entries], ["Section", "Label", "Value", "Default"], out)

    if page.links:
        out.write("\nLinks\n")
        print_rows([{
            "Label": link.label,
            "Kind": link.kind,
            "Page": link.page or "",
            "Href": link.href,
        } for link in page.links], ["Label", "Kind", "Page", "Href"], out)


def _select_options(select: ParsedSelect) -> str:
    detailed = ", ".join(
        option.value if option.label == option.value else f"{option.label}={option.value}"
        for option in (select.option_details or [])
    )
    return detailed or ", ".join(select.options)


def _quote_arg(value: str) -> str:
    return value if re.fullmatch(r"[a-z0-9_-]+", value, re.I) else json.dumps(value)


# --- form-state helpers -----------------------------------------------------------------------


def _form_state_values(
    page: ParsedPage, preferred_fields: Sequence[str], preferred_selects: Sequence[str]
) -> dict[str, str]:
    visible = [
        f for f in page.fields
        if _is_user_facing_field(f.name, f.type) and (f.type != "radio" or f.checked)
    ]
    ordered_fields = _order_by_preference(visible, preferred_fields, lambda f: f.name)
    ordered_selects = _order_by_preference(page.selects, preferred_selects, lambda s: s.name)
    values: dict[str, str | None] = {}
    for select in ordered_selects:
        values[_labelize(select.name)] = _on_off(select.value)
    for f in ordered_fields:
        values[_labelize(f.name)] = _yes_no(f.checked) if f.type == "checkbox" else f.value
    return _compact(values)


def _is_user_facing_field(name: str, field_type: str) -> bool:
    return name not in _HIDDEN_FIELDS and field_type != "hidden"


def _order_by_preference(items: Sequence[T], preferred: Sequence[str], name_of: Callable[[T], str]) -> list[T]:
    by_name = {name_of(item): item for item in items}
    preferred_items = [by_name[name] for name in preferred if name in by_name]
    preferred_set = set(preferred)
    rest = sorted((item for item in items if name_of(item) not in preferred_set), key=name_of)
    return [*preferred_items, *rest]


def _labelize(name: str) -> str:
    text = re.sub(r"^u_", "", name).replace("_", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), text)


def _field(page: ParsedPage, name: str) -> ParsedField | None:
    return next((f for f in page.fields if f.name == name), None)


def _field_value(page: ParsedPage, name: str) -> str:
    found = _field(page, name)
    return found.value if found else ""


def _select_by_name(page: ParsedPage, name: str) -> ParsedSelect | None:
    return next((s for s in page.selects if s.name == name), None)


def _select_value(page: ParsedPage, name: str) -> str:
    found = _select_by_name(page, name)
    return found.value if found else ""


def _checked_radio_value(page: ParsedPage, name: str) -> str:
    return next((f.value for f in page.fields if f.name == name and f.checked), "")


def _duration_fields(page: ParsedPage, prefix: str) -> str:
    parts = [_field_value(page, f"{prefix}{unit}") or "0" for unit in ("day", "hour", "min", "sec")]
    return "{}d {}h {}m {}s".format(*parts)


def _on_off(value: str) -> str:
    return {"on": "On", "off": "Off"}.get(value, value)


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _guest_access(value: str) -> str:
    return {"on": "Internet Only", "off": "Internet & Home LAN"}.get(value, value)


# ---------------------------------------------------------------------------------------------
# dump / diff / restore


def snapshot_summary_output(snapshot: Snapshot, path: str) -> dict[str, Any]:
    """--json dict for `dump`: path, meta and counts (never the secrets-bearing forms themselves)."""
    return {
        "path": path,
        "meta": to_json_dict(snapshot.meta),
        "services": len(snapshot.services),
        "forwards": len(snapshot.forwards),
        "reservations": len(snapshot.reservations),
        "forms": list(snapshot.forms),
        "tables": list(snapshot.tables),
    }


def print_snapshot_summary(snapshot: Snapshot, path: str, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    out.write(f"Dump written: {_line(path)}\n")
    out.write(f"Firmware: {_line(snapshot.meta.firmware or 'unknown')}\n")
    out.write(f"Services: {len(snapshot.services)}\n")
    out.write(f"Forwards: {len(snapshot.forwards)}\n")
    out.write(f"Reservations: {len(snapshot.reservations)}\n")
    out.write(f"Forms: {_line(', '.join(snapshot.forms) or 'none')}\n")
    out.write(f"Tables: {_line(', '.join(snapshot.tables) or 'none')}\n")


def print_snapshot_diff(diff: SnapshotDiff, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    if diff.identical and not diff.firmware_changed:
        out.write("No differences.\n")
        return
    if diff.identical:
        out.write("No configuration differences.\n")
    for s in diff.services.missing:
        out.write(f"- missing service {_format_service(s)}\n")
    for s in diff.services.extra:
        out.write(f"+ extra service {_format_service(s)}\n")
    for f in diff.forwards.missing:
        out.write(f"- missing forward {_line(f.service)} -> {_line(f.device_label)} ({_line(f.device_mac)})\n")
    for f in diff.forwards.extra:
        out.write(f"+ extra forward {_line(f.service)} -> {_line(f.device_label)} ({_line(f.device_mac)})\n")
    for r in diff.reservations.missing:
        out.write(f"- missing reservation {_line(r.ip)} for {_line(r.mac)}\n")
    for c in diff.reservations.changed:
        out.write(f"~ reservation {_format_reservation_change(c)}\n")
    for r in diff.reservations.extra:
        out.write(f"+ extra reservation {_line(r.ip)} for {_line(r.mac)}\n")
    for page, changes in diff.forms.items():
        out.write(f"{_line(page)}:\n")
        for change in changes:
            live = redact_value(change.field, change.live if change.live is not None else "<absent>", False)
            dump = redact_value(change.field, change.dump if change.dump is not None else "<absent>", False)
            out.write(f"  ~ {_line(change.field)}: {_line(live)} -> {_line(dump)}\n")
    if diff.firmware_changed:
        out.write("Firmware differs between dump and router.\n")


def _format_service(s: SnapshotService) -> str:
    return f"{_line(s.name)} {_line(s.protocol)} {s.ext_min_port}-{s.ext_max_port} -> {s.int_start_port}"


def _format_reservation_change(change: ReservationChange) -> str:
    return f"{_line(change.mac)}: {_line(change.live_ip)} -> {_line(change.dump_ip)}"


def display_diff(diff: SnapshotDiff, include_secrets: bool) -> dict[str, Any]:
    """--json view of a SnapshotDiff; form values redacted by field name unless include_secrets."""
    if include_secrets:
        return to_json_dict(diff)
    forms = {
        page: [
            FormFieldDiff(
                field=change.field,
                dump=None if change.dump is None else redact_value(change.field, change.dump, False),
                live=None if change.live is None else redact_value(change.field, change.live, False),
            )
            for change in changes
        ]
        for page, changes in diff.forms.items()
    }
    return to_json_dict(replace(diff, forms=forms))


def display_restore_steps(steps: Iterable[RestoreStep], include_secrets: bool) -> list[dict[str, Any]]:
    """--json view of a restore plan: rawPayload (full form state incl. nonce) dropped, assignments redacted."""
    shown = []
    for step in steps:
        assignments = (
            [_redact_assignment(a, include_secrets) for a in step.assignments]
            if step.assignments is not None else None
        )
        shown.append(to_json_dict(replace(step, raw_payload=None, assignments=assignments)))
    return shown


def _redact_assignment(assignment: str, include_secrets: bool) -> str:
    field_name, separator, value = assignment.partition("=")
    if not separator:
        return assignment
    return f"{field_name}={redact_value(field_name, value, include_secrets)}"


def print_restore_plan(steps: Iterable[RestoreStep], stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    for step in steps:
        out.write(f"[{step.order}] {_line(step.page)} {step.kind}: {_line(step.description)}\n")
        if step.blocked:
            out.write(f"    blocked: {_line(step.blocked)}\n")
        elif step.deferred is not None:
            out.write(f"    deferred: {_line(step.deferred.reason)}\n")
        if step.warning:
            out.write(f"    warning: {_line(step.warning)}\n")
        elif step.display_payload is not None:
            out.write(f"    payload: {_line(_compact_json(step.display_payload))}\n")
        if not step.blocked and step.follow_up is not None:
            # No live value is available at plan time: the Save control only appears on the entry page
            # the Allocate POST opens. The plan prints the button name; the executor substitutes the
            # value that page actually renders (e.g. "Save...").
            out.write(f"    then: {_line(_compact_json(follow_up_payload(step.follow_up)))}\n")


def print_restore_step_result(result: RestoreStepResult, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    location = f" -> {_line(result.location)}" if result.location else ""
    if result.status_code is not None:
        detail = f" ({result.status_code}{location})"
    elif result.error:
        detail = f" ({_line(result.error)})"
    else:
        detail = ""
    out.write(f"[{result.order}] {result.status} {_line(result.page)}{detail}\n")
