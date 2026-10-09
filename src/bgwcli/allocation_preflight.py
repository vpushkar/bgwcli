"""Check requested IP ownership before the restore mutates any configuration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any

from .client import PostWriteEvidence, RouterResponseError, observe_post, pool_full_metadata
from .devices import devices_from_ip_allocation_rows
from .errors import BgwError, RouterAuthError, RouterConnectionError, RouterSessionPoolFullError, UsageError
from .parser import extract_tables, looks_like_login, parse_devices, parse_page
from .restore import router_error_banner
from .snapshot import MAC_PATTERN, SnapshotReservation
from .types import Device, HttpResponse, ParsedPage, to_json_dict

RESCAN_SETTLE_SECONDS = 60.0
RESCAN_TIMEOUT_SECONDS = 180.0
RESCAN_POLL_SECONDS = 5.0
INITIAL_OWNERSHIP_MAX_AGE_SECONDS = 30.0


class OwnershipSnapshotPages(dict[str, ParsedPage]):
    """Invocation-local parsed snapshot with single-use private raw ownership evidence.

    Only mapping entries serialize. Parsed tables are never used as a substitute for raw
    structural validation, and ordinary mappings/library fetchers simply get a fresh read.
    """

    def __init__(self) -> None:
        super().__init__()
        self._allocation_response: HttpResponse | None = None
        self._allocation_client: Any = None
        self._allocation_captured_at = 0.0

    def allocation_reader(self, client: Any) -> _OwnershipCaptureClient:
        return _OwnershipCaptureClient(client, self)

    def take_allocation_response(self, client: Any) -> HttpResponse | None:
        response = self._allocation_response
        self._allocation_response = None
        if client is not self._allocation_client:
            return None
        age = monotonic() - self._allocation_captured_at
        return response if 0 <= age <= INITIAL_OWNERSHIP_MAX_AGE_SECONDS else None


class _OwnershipCaptureClient:
    """Observe the successful raw GET during this snapshot's IP Allocation fetch."""

    def __init__(self, client: Any, snapshot: OwnershipSnapshotPages) -> None:
        self._client = client
        self._snapshot = snapshot

    def get_cgi_page(self, page: str) -> HttpResponse:
        response = self._client.get_cgi_page(page)
        if page == "ipalloc" and response.status_code == 200:
            self._snapshot._allocation_response = response
            self._snapshot._allocation_client = self._client
            self._snapshot._allocation_captured_at = monotonic()
        return response


@dataclass(frozen=True)
class AllocationConflict:
    ip: str
    target_mac: str
    holder_mac: str
    holder_name: str
    holder_status: str
    fixed_allocation: bool = False


@dataclass(frozen=True)
class AllocationPreflight:
    conflicts: list[AllocationConflict]
    clear_payload: dict[str, str] | None
    requests: tuple[SnapshotReservation, ...] = ()


@dataclass
class AllocationRescanEvidence:
    clear_attempted: bool | None = False
    clear_response_received: bool | None = False
    clear_accepted: bool = False
    ownership_verified: bool = False


class _RescanDeadlineReached(Exception):
    """Stop before starting another ownership read after the rescan budget expires."""


class _OwnershipTerminalError(UsageError):
    """Authentication or terminal HTTP failures must not be retried as incomplete data."""


def allocation_preflight_report(
    preflight: AllocationPreflight | None = None,
    *,
    error: Exception | None = None,
    rescan: AllocationRescanEvidence | None = None,
) -> dict[str, Any]:
    """One public, structured description of ownership-dependent incomplete planning."""
    fixed = bool(preflight and any(conflict.fixed_allocation for conflict in preflight.conflicts))
    clear = preflight.clear_payload if preflight else None
    blocked = error is not None or fixed or clear is None
    rescan = rescan or AllocationRescanEvidence()
    if error is not None:
        if rescan.clear_attempted:
            reason = f"Clear attempted; allocation preflight failed: {error}. Verify gateway state before retrying."
        elif rescan.clear_attempted is None:
            reason = f"Clear attempt state unknown; allocation preflight failed: {error}. Verify before retrying."
        else:
            reason = f"Allocation preflight blocked before Clear: {error}"
    elif fixed:
        reason = "Fixed allocation belongs to another MAC; manual release is required before restoring."
    elif clear is None:
        reason = "Conflicting ownership cannot be cleared: Clear and Rescan is unavailable."
    else:
        reason = "Restore plan pending Clear and Rescan, ownership verification, and refreshed configuration pages."
    report = {
        "conflicts": to_json_dict(preflight.conflicts) if preflight else [],
        "clearPayload": clear,
        "clearAvailable": clear is not None,
        "rescanPlanned": not blocked,
        "rescanPerformed": rescan.clear_accepted,
        "clearAttempted": rescan.clear_attempted,
        "clearResponseReceived": rescan.clear_response_received,
        "ownershipVerified": rescan.ownership_verified,
        "planStatus": "blocked" if blocked else "pending",
        "planComplete": False,
        "reason": reason,
    }
    if error is not None:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        if isinstance(error, RouterSessionPoolFullError):
            waited_ms, retry_count = pool_full_metadata(error)
            report["error"].update(waitedMs=waited_ms, retryCount=retry_count)
    return report


def inspect_allocation_conflicts(
    client: Any,
    reservations: Sequence[SnapshotReservation],
    *,
    snapshot_pages: Mapping[str, ParsedPage] | None = None,
) -> AllocationPreflight:
    """Read both ownership sources, including offline entries; never use a degraded fallback."""
    return _inspect_allocation_conflicts(client, reservations, snapshot_pages=snapshot_pages)


def _inspect_allocation_conflicts(
    client: Any,
    reservations: Sequence[SnapshotReservation],
    *,
    deadline: float | None = None,
    snapshot_pages: Mapping[str, ParsedPage] | None = None,
) -> AllocationPreflight:
    if not reservations:
        return AllocationPreflight([], None)
    body = _read_ownership_page(client, "devices", deadline=deadline)
    tables = extract_tables(body)
    if not any(
        "MAC Address" in {cell for row in table for cell in row}
        and "IPv4 Address / Name" in {cell for row in table for cell in row}
        for table in tables
    ):
        raise UsageError("Cannot validate IP ownership: devices page has no recognizable device table.")
    _validate_device_ownership(tables, {reservation.ip for reservation in reservations})
    parsed = parse_page("devices", body, include_secrets=True)
    clear = next(
        (
            button
            for button in parsed.buttons
            if not button.disabled
            and (
                button.name.lower() == "clear"
                or any(
                    "clear" in candidate.lower() and "rescan" in candidate.lower()
                    for candidate in (button.name, button.value, button.label)
                )
            )
        ),
        None,
    )
    payload = {clear.name: clear.value or clear.label or clear.name} if clear and clear.name else None
    owners = parse_devices(body)
    cached = (
        snapshot_pages.take_allocation_response(client) if isinstance(snapshot_pages, OwnershipSnapshotPages) else None
    )
    allocation_body = (
        _validate_ownership_response(cached, "ipalloc")
        if cached is not None
        else _read_ownership_page(client, "ipalloc", deadline=deadline)
    )
    allocation_owners = _parse_allocation_ownership(allocation_body)
    fixed_owners = {
        (device.ip.strip(), device.mac.strip().lower())
        for device in allocation_owners
        if device.allocation and "fixed" in device.allocation.lower()
    }
    owners.extend(allocation_owners)
    return AllocationPreflight(_conflicts(owners, reservations, fixed_owners), payload, tuple(reservations))


def _parse_allocation_ownership(body: str) -> list[Device]:
    tables = extract_tables(body)
    headers = {"IPv4 Address / Name", "MAC Address", "Status", "Allocation"}
    ownership_tables = [table for table in tables if table and headers <= set(table[0])]
    if not ownership_tables:
        raise UsageError("Cannot validate IP ownership: IP Allocation page has no recognizable table.")
    if any(len(row) < len(table[0]) for table in ownership_tables for row in table[1:] if row):
        raise UsageError("Cannot validate IP ownership: IP Allocation page contains an incomplete row.")
    parsed = parse_page("ipalloc", body, include_secrets=True)
    return devices_from_ip_allocation_rows(parsed.tables)


def _validate_device_ownership(tables: list[list[list[str]]], requested_ips: set[str]) -> None:
    """Refuse matched address records the forgiving device parser would skip or merge."""
    for table in tables:
        key_value = any(row and row[0] in ("MAC Address", "IPv4 Address / Name") for row in table)
        if key_value:
            holder_mac = ""
            previous_ip = None
            for row in table:
                if not row:
                    continue
                if row[0] == "MAC Address":
                    holder_mac = row[1].strip() if len(row) == 2 else ""
                    previous_ip = None
                elif row[0] == "IPv4 Address / Name" and len(row) >= 2:
                    ip = row[1].split("/", 1)[0].strip()
                    matched_ip = ip if ip in requested_ips else previous_ip
                    if matched_ip in requested_ips and (
                        len(row) != 2 or previous_ip is not None or not MAC_PATTERN.fullmatch(holder_mac)
                    ):
                        raise UsageError(
                            f"Cannot validate IP ownership for {matched_ip}: incomplete device MAC record."
                        )
                    previous_ip = ip
            continue
        for row in table[1:]:
            matched_ips = {cell.split("/", 1)[0].strip() for cell in row} & requested_ips
            if matched_ips and (
                len(row) < 4
                or len(table[0]) < 4
                or table[0][3] != "MAC Address"
                or not MAC_PATTERN.fullmatch(row[3].strip())
            ):
                ip = sorted(matched_ips)[0]
                raise UsageError(f"Cannot validate IP ownership for {ip}: incomplete device MAC row.")


def rescan_allocation_conflicts(
    client: Any,
    preflight: AllocationPreflight,
    *,
    log: Callable[[str], None] = print,
    evidence: AllocationRescanEvidence | None = None,
) -> None:
    """Clear stale discovery state once, settle, and prove requested addresses are available."""
    evidence = evidence or AllocationRescanEvidence()
    if not preflight.conflicts:
        return
    _refuse_fixed_conflicts(preflight)
    if not preflight.clear_payload:
        raise UsageError("Cannot clear conflicting IP ownership: devices page has no enabled Clear and Rescan button.")
    log("IP allocation conflicts found; clearing and rescanning the gateway device list.")
    observed = PostWriteEvidence()
    try:
        with observe_post(client, observed):
            response = client.post_cgi_page("devices", preflight.clear_payload)
            observed.attempted = observed.response_received = True
    except BgwError:
        raise
    except Exception as exc:
        raise UsageError(f"Cannot clear and rescan device ownership: {exc}") from exc
    finally:
        evidence.clear_attempted = observed.attempted
        evidence.clear_response_received = observed.response_received
    if response.status_code != 200 and not 300 <= response.status_code < 400:
        raise UsageError(f"Clear and Rescan rejected with HTTP {response.status_code}.")
    if looks_like_login(response.body):
        raise UsageError("Cannot validate Clear and Rescan: gateway returned the login page.")
    error = router_error_banner(response.body)
    if error:
        raise UsageError(f"Clear and Rescan rejected: {error}")
    evidence.clear_accepted = True

    deadline = monotonic() + RESCAN_TIMEOUT_SECONDS
    log(f"Waiting {RESCAN_SETTLE_SECONDS:g}s for the device rescan before checking IP ownership.")
    sleep(RESCAN_SETTLE_SECONDS)
    requests = preflight.requests or tuple(
        SnapshotReservation(conflict.target_mac, conflict.ip) for conflict in preflight.conflicts
    )
    remaining_conflicts = preflight.conflicts
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            refreshed = _inspect_allocation_conflicts(client, requests, deadline=deadline)
        except _RescanDeadlineReached:
            break
        except _OwnershipTerminalError:
            raise
        except RouterSessionPoolFullError as exc:
            # A full session pool ends the rescan at once: it is never a disposable ownership read
            # that a later poll can replace. Further polls would only compete for the exhausted pool,
            # and the session coordinator records the cooldown only when this error reaches it.
            exc.args = (
                f"IP ownership unverifiable after Clear and Rescan: {exc}. "
                f"Last verified conflicts: {_conflict_details(remaining_conflicts)}",
            )
            raise
        except (UsageError, RouterConnectionError, RouterResponseError) as exc:
            # An invalid pair proves nothing about availability. Retain the last verified
            # conflicts and retry reads only, within the original budget after the one Clear.
            last_error = exc
        else:
            _refuse_fixed_conflicts(refreshed)
            remaining_conflicts = refreshed.conflicts
            last_error = None
            if not remaining_conflicts:
                evidence.ownership_verified = True
                log("Device rescan complete; requested IP addresses have no conflicting MAC ownership.")
                return
        remaining_seconds = deadline - monotonic()
        if remaining_seconds > 0:
            sleep(min(RESCAN_POLL_SECONDS, remaining_seconds))
    details = _conflict_details(remaining_conflicts)
    if last_error is not None:
        message = f"IP ownership unverifiable after Clear and Rescan: {last_error}. Last verified conflicts: {details}"
        raise UsageError(message) from last_error
    raise UsageError(f"IP allocation conflict after Clear and Rescan: {details}")


def _conflict_details(conflicts: Sequence[AllocationConflict]) -> str:
    return "; ".join(
        f"{conflict.ip} is still allocated to {conflict.holder_mac}"
        f" ({conflict.holder_name or 'unnamed'}, {conflict.holder_status or 'unknown status'});"
        f" requested for {conflict.target_mac}"
        for conflict in conflicts
    )


def _refuse_fixed_conflicts(preflight: AllocationPreflight) -> None:
    fixed = [conflict for conflict in preflight.conflicts if conflict.fixed_allocation]
    if fixed:
        details = "; ".join(f"{conflict.ip} belongs to {conflict.holder_mac}" for conflict in fixed)
        raise UsageError(f"Fixed allocation conflict: {details}; manual release is required before restoring.")


def _read_ownership_page(client: Any, page: str, *, deadline: float | None = None) -> str:
    if deadline is not None and monotonic() >= deadline:
        raise _RescanDeadlineReached
    try:
        response = client.get_cgi_page(page)
    except (RouterAuthError, RouterConnectionError, RouterResponseError):
        # Typed transport answers keep their identity: autorestore reports an HTTP error status
        # like a timeout (router-unreachable), not as a usage error.
        raise
    except Exception as exc:
        raise UsageError(f"Cannot read {page} to validate IP ownership: {exc}") from exc
    return _validate_ownership_response(response, page)


def _validate_ownership_response(response: HttpResponse, page: str) -> str:
    if response.status_code in (401, 403):
        raise _OwnershipTerminalError(f"Cannot validate IP ownership: {page} returned HTTP {response.status_code}.")
    if response.status_code != 200:
        raise UsageError(f"Cannot validate IP ownership: {page} returned HTTP {response.status_code}.")
    if looks_like_login(response.body):
        raise _OwnershipTerminalError(f"Cannot validate IP ownership: {page} returned the login page.")
    error = router_error_banner(response.body)
    if error:
        raise UsageError(f"Cannot validate IP ownership: {page} reported {error}")
    return response.body


def _conflicts(
    devices: Sequence[Device], reservations: Sequence[SnapshotReservation], fixed_owners: set[tuple[str, str]]
) -> list[AllocationConflict]:
    conflicts = []
    seen = set()
    for reservation in reservations:
        target_mac = reservation.mac.strip().lower()
        for device in devices:
            if device.ip.strip() != reservation.ip:
                continue
            holder_mac = device.mac.strip().lower()
            if not MAC_PATTERN.fullmatch(holder_mac):
                raise UsageError(
                    f"Cannot validate IP ownership for {reservation.ip}: holder MAC '{device.mac}' is invalid."
                )
            ownership = (reservation.ip, target_mac, holder_mac)
            if holder_mac != target_mac and ownership not in seen:
                conflicts.append(
                    AllocationConflict(
                        reservation.ip,
                        target_mac,
                        holder_mac,
                        device.name,
                        device.status,
                        (reservation.ip, holder_mac) in fixed_owners,
                    )
                )
                seen.add(ownership)
    return conflicts
