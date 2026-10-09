"""Command-line entry point: argparse subcommands, option resolution, dispatch and exit codes.

Port of BGW320-CLI src/cli.ts. Kept deliberately thin: parse -> env defaults + flags -> build the
client -> run the command (inside the session coordinator unless it is purely local) -> render via
format.* or --json -> exit code. Exit codes follow exit_codes.py: 0 ok, 1 negative answer (diff
differs, restore not converged, usage/refusals), 2 could-not-answer (auth, connection, pool full,
dump file, snapshot extraction, page unavailable).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from pathlib import Path
from time import sleep
from typing import Any

from . import __version__
from . import format as fmt
from .actions import ROUTER_ACTIONS, display_action_payload, get_action, is_opener_button
from .allocation_preflight import (
    AllocationRescanEvidence,
    OwnershipSnapshotPages,
    allocation_preflight_report,
    inspect_allocation_conflicts,
    rescan_allocation_conflicts,
)
from .audit import build_audit, capture_fixture_pack, preflight_fixture_root
from .autorestore import AutorestoreOptions, result_output, run_autorestore
from .client import (
    BGW320Client,
    PostWriteEvidence,
    RouterResponseError,
    observe_post,
    pool_full_metadata,
    session_pool_full_error,
)
from .config import DEFAULT_HOST, GlobalOptions, env_default_options, resolve_access_code
from .devices import fetch_device_list
from .diagnostics import (
    DIAG_CONFIRM_TOKEN,
    build_diagnostic_plan,
    diagnostic_button,
    extract_diagnostic_result,
    is_diagnostic_kind,
    poll_diagnostic_result,
)
from .dumpfile import (
    default_dump_path,
    preflight_dump_target,
    read_dump_file,
    snapshot_problem,
    write_dump_file,
)
from .errors import (
    BgwError,
    RouterAuthError,
    RouterConnectionError,
    RouterSessionPoolFullError,
    SnapshotExtractionError,
    UsageError,
    is_page_level_auth_error,
)
from .exit_codes import INTERRUPTED, NEGATIVE_ANSWER, NO_ANSWER, fatal_exit_code
from .fetch import LOGIN_PAGE_ERROR, ParsedPageResult, fetch_parsed_page, is_page_not_found
from .mutations import DANGEROUS_PAGES, build_mutation_plan, build_submit_plan, confirm_token_for_page
from .operations import (
    OperationResult,
    action_committed,
    action_dry_run,
    diagnostic_committed,
    diagnostic_dry_run,
    restore_committed,
    restore_dry_run,
    set_committed,
    set_dry_run,
    submit_committed,
    submit_dry_run,
)
from .pages import list_sections, resolve_cgi_page, resolve_section_command, router_tabs, tabs_for_section
from .parser import looks_like_login, parse_logs, parse_page, parse_sitemap
from .recovery_state import RecoveryCheckpoint
from .redact import page_sensitive_names, redact_value
from .restore import (
    VERIFY_BEFORE_RETRYING,
    WRITE_REJECTED_PREFIX,
    RestoreOptions,
    RestorePostcondition,
    RestoreStep,
    RestoreStepResult,
    append_sentence,
    build_restore_plan,
    confirm_saved_page,
    confirm_warning_page,
    execute_restore,
    is_confirmation_redirect,
    is_transient_verification_status,
    pending_allocation_requests,
    reconnect_verification_reason,
    redirect_answer_page,
    restore_converged,
    router_error_banner,
    sent_guidance,
    write_guidance,
)
from .save_confirmation import no_changes_notice, no_changes_text, save_notification
from .save_result import (
    NO_CHANGES_TEXT,
    UNCONFIRMED_TEXT,
    decide_save_result,
    decision_for_step,
    observed_no_change,
)
from .session import (
    SessionCoordinatorOptions,
    clear_session_state,
    read_session_state,
    record_pool_full_cooldown,
    router_session_identity,
    with_router_session,
)
from .snapshot import (
    BEST_EFFORT_PAGES,
    IPV4_PATTERN,
    OPTIONAL_FORM_PAGES,
    RESERVATION_COLUMNS,
    SNAPSHOT_PAGES,
    Snapshot,
    duplicate_service_names,
    extract_snapshot,
    find_column,
    missing_form_controls,
    missing_table_structure,
    snapshot_pages_for,
    snapshot_read_pages,
    truncated_page_problem,
)
from .snapshot import resolve_include as resolve_dump_include
from .snapshot_diff import diff_snapshots, pages_missing_from_dump
from .snapshot_diff import resolve_include as resolve_restore_include
from .sweep import (
    SweepOptions,
    SweepProgressEvent,
    preflight_sweep_output,
    strip_large_payloads,
    sweep_exit_code,
    sweep_router,
    write_sweep_artifacts,
)
from .terminal import printable_error, sanitize_terminal_text
from .types import ParsedPage
from .write_outcome import unanswered_write_step

USER_AGENT = f"bgw/{__version__}"
# Delays before the 2nd and 3rd post-save verification re-read (3 attempts total).
VERIFY_RETRY_DELAYS: tuple[float, ...] = (2.0, 4.0)
RESTORE_CONFIRM_TOKEN = "RESTORE"
# autorestore: the printed sticker code the gateway reverts to after a factory reset.
FALLBACK_ACCESS_CODE_ENV = "BGW_FALLBACK_ACCESS_CODE"

PAGE_FETCH_FAILED = 2

SECTION_COMMANDS: dict[str, tuple[str, ...]] = {
    "device": (),
    "broadband": (),
    "home-network": ("home", "lan"),
    "voice": (),
    "firewall": (),
    "diagnostics": ("diagnostic", "diag"),
}
LOCAL_COMMANDS = frozenset({"actions", "coverage", "section", "session", "sitemap", "tabs"})
COMMAND_NAMES: tuple[str, ...] = (
    "check", "auth", "sitemap", "coverage", "tabs", "actions", "session", "action", "sweep", "scan", "schema",
    "audit", "readiness", "section", *SECTION_COMMANDS, "page", "inspect", "devices", "wifi", "nat", "logs",
    "set", "submit", "status", "dump", "diff", "restore", "autorestore", "help", "fixtures-capture",
)

# Injection seam for tests: (options, access_code, *, on_session_wait, user_agent) -> client.
_client_factory: Callable[..., Any] = BGW320Client.from_options


@dataclass
class Command:
    name: str
    args: list[str]
    options: GlobalOptions
    raw: bool = False
    forms: bool = False
    all: bool = False
    commit: bool = False
    full: bool = False
    confirm: str | None = None
    access_code: str | None = None  # resolved once at client build; reused so stdin is never re-read
    protocol: str | None = None
    delay_ms: int = 750
    limit: int = 20
    include_parsed: bool = False
    pages: list[str] | None = None
    out_dir: str | None = None
    prune: bool = False
    all_clients: bool = False
    # --include <csv|all>: dump = optional form pages to capture besides the core; diff/restore = the
    # pages to compare/restore (None = everything present in the dump). Resolved per command.
    include: list[str] | None = None
    # autorestore: restore passes, seconds between passes, treat any restorable difference as a reset.
    max_passes: int = 3
    wait_seconds: int = 120
    on_any_diff: bool = False
    exit_code: int = field(default=0, compare=False)
    # Clients other than the one `run` built that performed (part of) the command: autorestore's
    # fallback-code client. An interrupt reads the write evidence of all of them.
    evidence_clients: list[Any] = field(default_factory=list, compare=False)

    def output(self, value: Any, table_printer: Callable[[], None]) -> None:
        if self.options.json:
            fmt.print_json(value)
        else:
            table_printer()


# ---------------------------------------------------------------------------------------------
# entry points


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    try:
        command = parse_args(args)
        if command.name == "help":
            sys.stdout.write(help_text())
            return 0
        return run(command)
    except RouterSessionPoolFullError as error:
        waited_ms, retry_count = pool_full_metadata(error)
        if json_mode:
            fmt.print_json({
                "ok": False,
                "page": "login",
                "error": str(error),
                "sessionPoolFull": True,
                "waitedMs": waited_ms,
                "retryCount": retry_count,
                "exitCode": NO_ANSWER,
                **_raised_write_evidence(error),
            })
        else:
            sys.stderr.write(f"{printable_error(error)}\n")
        return NO_ANSWER
    except KeyboardInterrupt as interrupt:
        return _report_interrupt(interrupt, json_mode)
    except Exception as error:  # noqa: BLE001 - every error is reported through fatal_exit_code (TS parity)
        # Exit 2 keeps `diff` honest: exit 1 means "the router differs from the dump", never "the dump
        # could not be read" or "the live pages could not be turned into a snapshot".
        code = fatal_exit_code(error)
        if json_mode:
            # JSON callers get a structured answer on stdout for every outcome; stderr keeps the text.
            fmt.print_json({
                "ok": False,
                "error": str(error),
                "exitCode": code,
                "errorType": type(error).__name__,
                **_raised_write_evidence(error),
            })
        # A usage message is the CLI's own multi-line text; every other error may carry router text.
        message = str(error) if isinstance(error, UsageError) else printable_error(error)
        sys.stderr.write(f"{message}\n")
        return code


def _report_interrupt(interrupt: KeyboardInterrupt, json_mode: bool) -> int:
    """Ctrl-C: exit 130 with a one-line stderr note and, with --json, a structured object. When a
    configuration POST may have been sent, the answer says the write state is unknown."""
    evidence = _raised_write_evidence(interrupt)
    if evidence.get("writeAttempted"):
        message = (
            "Interrupted: a configuration write may have been sent and its state is unknown; "
            "re-read the gateway before retrying."
        )
    else:
        message = "Interrupted: no configuration write had been sent."
    if json_mode:
        fmt.print_json({
            "ok": False, "error": message, "exitCode": INTERRUPTED, "errorType": "Interrupted", **evidence,
        })
    sys.stderr.write(f"{message}\n")
    return INTERRUPTED


def _attach_interrupt_evidence(interrupt: KeyboardInterrupt, client: Any, *others: Any) -> None:
    """Record what the transport saw of configuration POSTs on the interrupt, so the report can say
    whether a write may have gone out (the counter moves before the transport runs). `others` are the
    clients that performed the operation instead of `client` (the fallback-code login): each counts
    its own POSTs, so the evidence is read from all of them and a client with a sent POST wins (the
    last such one, being the client the operation ended on); with none, the first readable one."""
    chosen: tuple[int, Any] | None = None
    for candidate in (client, *others):
        observe = getattr(candidate, "observe_writes", None)
        try:
            observed = observe() if callable(observe) else None
            count = observed.attempts if observed is not None and type(observed.attempts) is int else None
        except Exception:  # noqa: BLE001 - instrumentation never replaces the interrupt
            count = None
        if count is None:
            continue
        if chosen is None or count > 0:
            chosen = (count, observed)
    if chosen is None:
        return
    attempts, seen = chosen
    interrupt.write_attempted = attempts > 0
    if attempts > 0:
        answered = seen.responses == attempts
        interrupt.write_evidence = {
            "writeResponseReceived": answered, "writeAttempts": attempts,
            **({"statusCode": seen.last_status} if answered and type(seen.last_status) is int else {}),
        }


def _raised_write_evidence(error: BaseException) -> dict[str, Any]:
    """Write evidence (`writeAttempted`, and when known the acknowledgement/commit state) for an error
    raised after a write POST was observed (absent otherwise)."""
    evidence: dict[str, Any] = {}
    if hasattr(error, "write_attempted"):
        evidence["writeAttempted"] = error.write_attempted
    # An acknowledged write whose verification then lost the session keeps what was observed.
    evidence.update(getattr(error, "write_evidence", None) or {})
    return evidence


def run(command: Command, client: Any | None = None) -> int:
    """Build the client (unless injected) and run the command, inside the session coordinator when needed."""
    if client is None:
        access_code = resolve_access_code(command.options)
        command.access_code = access_code
        on_session_wait = None if command.options.json else _print_session_wait
        client = _client_factory(command.options, access_code, on_session_wait=on_session_wait, user_agent=USER_AGENT)

    try:
        if not uses_session_coordinator(command.name):
            _run_uncoordinated(client, command)
            return command.exit_code

        coordinator = SessionCoordinatorOptions.from_options(command.options)
        with_router_session(client, coordinator, lambda: run_command(client, command))
        return command.exit_code
    except KeyboardInterrupt as interrupt:
        _attach_interrupt_evidence(interrupt, client, *command.evidence_clients)
        raise


def _run_uncoordinated(client: Any, command: Command) -> None:
    """The local commands (sitemap, coverage) read public pages outside the session coordinator; a full
    pool they observe still records its cooldown before the error propagates."""
    try:
        run_command(client, command)
    except RouterSessionPoolFullError as error:
        record_pool_full_cooldown(client, error, SessionCoordinatorOptions.from_options(command.options))
        raise


def uses_session_coordinator(name: str) -> bool:
    return name not in LOCAL_COMMANDS


def _print_session_wait(event: dict[str, int]) -> None:
    interval = max(1, event["intervalMs"])
    total_retries = max(1, -(-event["timeoutMs"] // interval))
    sys.stderr.write(
        f"Router web session pool is full; waiting {event['intervalMs']}ms before retry "
        f"{event['retryCount'] + 1} of approximately {total_retries}.\n"
    )


# ---------------------------------------------------------------------------------------------
# dispatch


def run_command(client: Any, command: Command) -> dict[str, Any] | None:  # noqa: C901 - one flat switch like cli.ts
    name = command.name
    opts = command.options

    if name == "check":
        result = client.check()
        if result.get("authenticated"):
            # A held (possibly cached) session is only reported once the gateway accepted it.
            result = {**result, "authenticated": _session_accepted(client)}
        if not result["reachable"]:
            command.exit_code = NO_ANSWER  # the gateway did not answer: no verdict on anything else
        command.output(result, lambda: fmt.print_check_result(result["host"], result["reachable"], result["title"]))
        return

    if name == "auth":
        # Always a real login: a cached session would otherwise "verify" a code nobody checked.
        client.login(force=True)
        command.output({"authenticated": True}, lambda: sys.stdout.write("authenticated\n"))
        return

    if name == "sitemap":
        entries = _read_sitemap(client, command)
        if entries is None:
            return
        command.output(entries, lambda: fmt.print_sitemap(entries))
        return

    if name == "coverage":
        entries = _read_sitemap(client, command)
        if entries is None:
            return
        live_pages = [entry.page for entry in entries]
        result = fmt.coverage_output(mapped_pages=[tab.page for tab in router_tabs], live_pages=live_pages)
        command.output(result, lambda: fmt.print_coverage(result))
        return

    if name == "tabs":
        command.output(list(router_tabs), lambda: fmt.print_tabs(router_tabs))
        return

    if name == "actions":
        command.output(list(ROUTER_ACTIONS), lambda: fmt.print_actions(ROUTER_ACTIONS))
        return

    if name == "session":
        _run_session(command)
        return

    if name == "action":
        _run_action(client, command)
        return

    if name in ("sweep", "scan", "schema"):
        scans = _run_sweep(client, command)
        if command.raw and not command.out_dir and not opts.json:
            return
        command.output(scans, lambda: fmt.print_scans(scans))
        return

    if name in ("audit", "readiness"):
        if command.raw:
            # The audit is a compact health summary; it never carries page HTML.
            raise UsageError(f"--raw is not supported by {name}. Use `bgwcli sweep --pages <page> --raw`.")
        audit = build_audit(_run_sweep(client, command, force_compact=True))
        command.output(audit, lambda: fmt.print_audit(audit))
        return

    if name == "fixtures-capture":
        _run_fixture_capture(client, command)
        return

    if name == "section":
        section = _require_arg(command, "section name")
        tabs = tabs_for_section(section)
        if not tabs:
            raise UsageError(f"Unknown section: {section}")
        command.output(tabs, lambda: fmt.print_tabs(tabs))
        return

    if name == "diagnostics" and command.args and is_diagnostic_kind(command.args[0]):
        _run_diagnostic(client, command)
        return

    if name in SECTION_COMMANDS:
        tab = resolve_section_command(name, command.args)
        if tab is None:
            if name == "diagnostics":
                # cli.ts:251 has a dedicated `diagnostics` case with its own message.
                raise UsageError(f"Unknown diagnostics command: {' '.join(command.args) or '(none)'}")
            # Every other section root falls through to the TS default switch case (cli.ts:473).
            raise UsageError(f"Unknown command: {name}\nRun bgwcli help.")
        _print_page(client, command, tab.page, forms=command.forms)
        return

    if name in ("page", "inspect"):
        page = resolve_cgi_page(" ".join(command.args) or _require_arg(command, "page name"))
        _print_page(client, command, page, forms=name == "inspect" or command.forms)
        return

    if name in _DIRECT_PAGES:
        _print_page(client, command, _DIRECT_PAGES[name], forms=command.forms)
        return

    if name == "set":
        _run_set(client, command)
        return

    if name == "submit":
        _run_submit(client, command)
        return

    if name == "status":
        from .status import fetch_status_sections, status_answered

        sections = fetch_status_sections(client, opts.include_secrets)
        if not status_answered(sections):
            command.exit_code = NO_ANSWER  # no section could be read
        command.output(sections, lambda: fmt.print_status_sections(sections))
        return

    if name == "dump":
        _run_dump(client, command)
        return

    if name == "diff":
        _run_diff(client, command)
        return

    if name == "restore":
        return _run_restore(client, command)

    if name == "autorestore":
        return _run_autorestore(client, command)

    raise UsageError(f"Unknown command: {name}\nRun bgwcli help.")


_DIRECT_PAGES = {"devices": "devices", "wifi": "wconfig_unified", "nat": "nattable", "logs": "logs"}


def _read_sitemap(client: Any, command: Command) -> list[Any] | None:
    """The live sitemap's entries, or None after reporting an HTTP error answer as an unavailable
    page (exit 2): an error page parsed as a sitemap would read as an empty or bogus answer."""
    response = client.get_cgi_page("sitemap", auth=False)
    if not 200 <= response.status_code < 300:
        error = f"Router rejected {response.url} with HTTP {response.status_code}."
        failure = ParsedPageResult("sitemap", False, status_code=response.status_code, error=error)
        _report_fetch_failure(command, failure)
        return None
    if _unavailable_answer(command, "sitemap", response):
        return None
    entries = parse_sitemap(response.body)
    if not entries:
        # A "Please wait" document, an empty or truncated body: no live page was listed, which is no
        # answer (the logs table-header rule), never a gateway with zero pages.
        error = "the sitemap answered without any /cgi-bin/*.ha link; it is not read as an empty sitemap"
        _report_fetch_failure(
            command, ParsedPageResult("sitemap", False, response.status_code, error=error, structural=True)
        )
        return None
    return entries


def _unavailable_answer(command: Command, page: str, response: Any) -> bool:
    """Report (exit 2) a 200 answer that is a "Page not found" document or the Login page rather than
    the page: it is never data. True when reported."""
    if looks_like_login(response.body):
        error = LOGIN_PAGE_ERROR
    elif is_page_not_found(parse_page(page, response.body)):
        error = "Page not found"
    else:
        return False
    _report_fetch_failure(command, ParsedPageResult(page, False, response.status_code, error=error))
    return True


def _session_accepted(client: Any) -> bool:
    """True when the gateway serves a protected page to the held session cookies. `check` never
    logs in: a Login-page answer, an HTTP error or a transport fault reads as not authenticated.
    A Login-page answer (or a redirect to login.ha) and a 401/403 are a verdict: the session is
    cleared, so the session coordinator forgets the dead cached copy instead of persisting it (or a
    cookie login.ha set while answering). A full session pool is no answer about the session: with
    --wait-for-session the client's wait loop polls until a slot frees and the session is probed
    once more; otherwise, or when the wait budget runs out, it propagates (exit 2, cooldown recorded).
    The client's own `probe_session_accepted` implements this."""
    return bool(client.probe_session_accepted())


# ---------------------------------------------------------------------------------------------
# command bodies


def _run_session(command: Command) -> None:
    action = command.args[0] if command.args else "status"
    origin = router_session_identity(command.options.host)
    if action == "status":
        state = read_session_state(
            origin, cache_ttl_ms=command.options.session_cache_ttl_ms,
            pool_cooldown_ms=command.options.session_pool_cooldown_ms,
        )
        command.output(state, lambda: fmt.print_session_state(state))
        return
    if action == "clear-cache":
        clear_session_state(origin, command.options.session_lock_timeout_ms)
        command.output({"ok": True, "cleared": True}, lambda: sys.stdout.write("session cache cleared\n"))
        return
    raise UsageError(f"Unknown session command: {action}")


OPENED_TEXT = "The button only opened an editor on the gateway; nothing was saved."


def _fetch_form_page(client: Any, page: str) -> ParsedPageResult:
    """Read `page` for a write plan. A form page that answered without any form control (a "Please
    wait" document, a truncated body) is a failed structural read, never a form with no fields: a
    plan built from it would post a lone field with no Save value, nonce or sibling fields."""
    result = fetch_parsed_page(client, page, include_secrets=True)
    if result.ok and result.parsed is not None:
        unreadable = truncated_page_problem(page, result.parsed) or missing_form_controls(
            page, result.parsed, any_page=True
        )
        if unreadable is not None:
            return ParsedPageResult(page, False, result.status_code, error=unreadable, structural=True)
    return result


def _run_action(client: Any, command: Command) -> None:
    name = _require_arg(command, "action name")
    action = get_action(name)
    if action is None:
        raise UsageError(f"Unknown action: {name}")
    payload = display_action_payload(action, command.options.include_secrets)

    if not command.commit:
        result = action_dry_run(action, payload)
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    if command.confirm != action.confirm_token:
        raise UsageError(
            f"Refusing to run action '{action.name}'. Re-run with --commit --confirm {action.confirm_token}."
        )

    # Every action reads its source page first: a "Please wait" or truncated answer carries no form
    # (and no nonce), so it is refused before any POST.
    page_result = _fetch_form_page(client, action.page)
    if not page_result.ok or page_result.parsed is None:
        _report_fetch_failure(command, page_result)
        return
    sent = PostWriteEvidence()
    if action.form_button:
        # The button lives inside the page's main form: post the live base payload + the button.
        # A submit plan is never blocked (dangerous pages are allowed for explicit buttons) and a
        # missing button raises, so the plan is always postable.
        plan = build_submit_plan(
            action.page, page_result.parsed, action.form_button, [], command.options.include_secrets
        )
        outcome = _post_and_confirm(
            client, action.page, plan.raw_payload, read_redirect=not action.drops_web_server, evidence=sent
        )
    elif action.post_path:
        post_path = action.post_path
        outcome = _post_observed(
            client, action.page, lambda: client.post_form(action.page, post_path, action.payload),
            describe_raised=True, evidence=sent,
        )
    else:
        outcome = _post_observed(
            client, action.page, lambda: client.post_cgi_page(action.page, action.payload),
            describe_raised=True, evidence=sent,
        )
    resent = _resent_attempts(sent)
    if not isinstance(outcome, RestoreStepResult | tuple):
        # The gateway's notification decides the action like every other write path: a rejection
        # banner is a structured rejection (exit 1); "No changes detected" is unchanged (exit 0).
        outcome = _answer_step(
            client, action.page, outcome, read_redirect=not action.drops_web_server, attempts=resent
        ) or (outcome.status_code, _location(outcome.headers))
    if isinstance(outcome, RestoreStepResult):
        if resent is not None and outcome.write_attempts is None:
            outcome = replace(outcome, write_attempts=resent)
        if outcome.status == "unchanged":
            result = replace(
                _with_save_evidence(action_committed(action, None, None), outcome),
                committed=False, outcome="unchanged", result=f"{NO_CHANGES_TEXT}.",
            )
            command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
            return
        _report_failed_write(command, action_committed(action, None, None), outcome)
        return
    status_code, location = outcome
    result = replace(action_committed(action, status_code, location), write_attempts=resent)
    if action.drops_web_server:
        result = replace(
            result, answer_read=False, write_attempted=True, write_response_received=True,
            result=(
                "The gateway drops its web server or this client's radio for this action, "
                "so its answer page was not read."
            ),
        )
    if action.opener:
        result = replace(
            result, committed=False, outcome="opened", write_attempted=True, write_response_received=True,
            write_performed=False, acknowledgement_observed=False, result=OPENED_TEXT,
        )
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


def _run_diagnostic(client: Any, command: Command) -> None:
    kind = command.args[0]
    if len(command.args) < 2 or not command.args[1]:
        raise UsageError(f"Missing target for diagnostics {kind}.")
    target = command.args[1]
    if command.commit and command.confirm != DIAG_CONFIRM_TOKEN:
        raise UsageError(f"Refusing to run diagnostic '{kind}'. Re-run with --commit --confirm {DIAG_CONFIRM_TOKEN}.")

    diag_page = fetch_parsed_page(client, "diag", include_secrets=True)
    truncated = truncated_page_problem("diag", diag_page.parsed) if diag_page.ok else None
    if truncated is not None:
        diag_page = ParsedPageResult("diag", False, diag_page.status_code, error=truncated, structural=True)
    if not diag_page.ok or diag_page.parsed is None:
        _report_fetch_failure(command, diag_page)
        return
    plan = build_diagnostic_plan(
        diag_page.parsed, kind, target, command.protocol, command.options.include_secrets
    )

    if not command.commit:
        result = diagnostic_dry_run(kind, target, plan.display_payload, diagnostic_button(kind))
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    sent = PostWriteEvidence()
    response = _post_observed(
        client, "diag", lambda: client.post_cgi_page("diag", plan.raw_payload), describe_raised=True,
        evidence=sent,
    )
    if isinstance(response, RestoreStepResult):
        _report_failed_write(command, diagnostic_committed(kind, target, None, None), response)
        return
    resent = _resent_attempts(sent)
    redirect_bodies: list[str] = []
    banner = _answer_banner(client, "diag", response, redirect_bodies, attempts=resent)
    if isinstance(banner, RestoreStepResult):
        _report_failed_write(command, diagnostic_committed(kind, target, response.status_code, None), banner)
        return
    if banner:
        # The gateway refused the diagnostic form (inline banner on a 200, or on the redirect target):
        # a structured rejection (exit 1), not a run to poll.
        step = _rejection_step("diag", response.status_code, _location(response.headers), banner, resent)
        _report_failed_write(command, diagnostic_committed(kind, target, response.status_code, None), step)
        return
    result_page = parse_page("diag", response.body, include_secrets=command.options.include_secrets)
    text = extract_diagnostic_result(result_page)
    if not text:
        # The gateway runs diagnostics asynchronously: the POST answers 302 with an empty body and the
        # ProgressWindow fills a few seconds later. Poll the page until the output is present and stable.
        try:
            # The redirect target read for the banner is the first result page, never discarded.
            text, polled = poll_diagnostic_result(
                client, include_secrets=command.options.include_secrets,
                initial_body=redirect_bodies[0] if redirect_bodies else None,
            )
        except (RouterResponseError, RouterConnectionError, RouterAuthError) as exc:
            if isinstance(exc, RouterAuthError) and not is_page_level_auth_error(exc):
                # Session lost or pool full after the diagnostic POST was answered: it still raises,
                # carrying the evidence that the diagnostic was sent once.
                exc.args = (_write_evidence_guidance(str(exc), sent),)
                exc.write_attempted = True
                if sent.response_received is True:
                    exc.write_evidence = {**(getattr(exc, "write_evidence", None) or {}), "writeResponseReceived": True}
                _add_write_attempts(exc, resent)
                raise
            _report_unread_diagnostic(command, kind, target, response, result_page, exc, resent)
            return
        if polled is not None:
            result_page = polled
    operation = diagnostic_committed(
        kind, target, response.status_code,
        text
        or (
            # Starting the diagnostic was the write, and the gateway accepted it (exit 0); its output
            # simply did not appear within the poll window.
            f"The diagnostic was started (router returned status {response.status_code}); its result is "
            "not yet available. Run 'bgwcli page diag' to read it later."
        ),
    )
    operation = replace(operation, write_attempts=resent)
    command.output(
        {**fmt.operation_output(operation), "resultAvailable": bool(text),
         "pageResult": fmt.parsed_page_output(result_page)},
        lambda: fmt.print_operation(operation),
    )


def _report_unread_diagnostic(
    command: Command, kind: str, target: str, response: Any, result_page: Any, error: Exception,
    attempts: int | None = None,
) -> None:
    """The diagnostic POST was answered, so the run started, but reading its result failed: the run
    is reported as committed with the POST's evidence, and the missing result is "no answer" (exit 2).
    `attempts` is the POST count of a diagnostic re-sent after a Login-page answer."""
    started = (
        "started once" if attempts is None
        else f"started {attempts} times (a Login-page answer forces one re-login and re-send)"
    )
    operation = replace(
        diagnostic_committed(kind, target, response.status_code, None),
        outcome="applied", location=_location(response.headers),
        warning=append_sentence(
            f"could not read the diagnostic result: {error}",
            f"The diagnostic was {started}; run 'bgwcli page diag' to read its output later.",
        ),
        write_attempted=True, write_response_received=True, write_attempts=attempts,
    )
    command.exit_code = NO_ANSWER
    command.output(
        {**fmt.operation_output(operation), "pageResult": fmt.parsed_page_output(result_page)},
        lambda: fmt.print_operation(operation),
    )


def _has_settable_control(parsed: Any) -> bool:
    """True when the page carries an input other than a hidden one (buttons are never in `fields`),
    a select or a textarea: the controls `set` can change."""
    return bool(
        parsed.selects or parsed.textareas
        or any(f.type.lower() != "hidden" for f in parsed.fields)
    )


def _run_set(client: Any, command: Command) -> None:
    page = resolve_cgi_page(_require_arg(command, "page name"))
    assignments = command.args[1:]
    if not assignments:
        raise UsageError("Missing KEY=VALUE assignment.")
    confirmation = confirm_token_for_page(page)
    if command.commit:
        if page in DANGEROUS_PAGES:
            raise UsageError(
                f"Refusing to mutate dangerous page '{page}'. Use an explicit action command if supported."
            )
        if command.confirm != confirmation:
            raise UsageError(f"Refusing to commit changes to '{page}'. Re-run with --commit --confirm {confirmation}.")

    page_result = _fetch_form_page(client, page)
    if not page_result.ok or page_result.parsed is None:
        _report_fetch_failure(command, page_result)
        return
    if not _has_settable_control(page_result.parsed):
        if page == "ipalloc":
            # The reservation editor is closed: `submit ipalloc Allocate_<n>` opens it, and its
            # `alloc_<mac>` select is what `set` then changes.
            raise UsageError(
                "The ipalloc page has no settable fields until an allocation editor is open (only buttons "
                "or hidden inputs are shown). Run 'submit ipalloc Allocate_<n>' to open the editor, then "
                "'set ipalloc alloc_<mac>=<ip>'."
            )
        raise UsageError(
            f"The {page} page has no settable fields (only buttons or hidden inputs), so there is "
            f"nothing for 'set' to change. Use 'submit {page} <button>' or 'action' for its buttons."
        )
    plan = build_mutation_plan(page, page_result.parsed, assignments, command.options.include_secrets)
    if plan.blocked:
        raise UsageError(plan.reason or "Mutation blocked.")

    if not command.commit:
        result = set_dry_run(plan, confirmation)
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    if _report_wifi_save(client, command, page, plan, "set"):
        return
    if _report_lan_save(client, command, page, plan, page_result.parsed, "set"):
        return
    sent = PostWriteEvidence()
    status_code, location, saved = _confirm_save(client, page, plan.raw_payload, sent)
    if saved is not None:
        saved = _acknowledged_write_as_applied(saved)
    _report_save(client, command, page, plan, saved, "set", status_code=status_code, location=location,
                 write_attempts=_resent_attempts(sent))


def _requested_values(plan: Any) -> bool:
    return bool(getattr(plan, "display_changes", None))


@dataclass(frozen=True)
class _Verification:
    """Outcome of the post-save live re-read. `read_error` is set only when a re-read was attempted
    and could not complete (it decides the exit code); `plan_warning` is the mutation plan's own
    advisory text and never changes the exit code. `attempts` is None when no re-read ran."""
    verified: bool | None = None
    mismatches: dict[str, dict[str, str]] | None = None
    read_error: str | None = None
    plan_warning: str | None = None
    attempts: int | None = None

    @property
    def warning(self) -> str | None:
        return " ".join(text for text in (self.read_error, self.plan_warning) if text) or None


def _save_verification(
    client: Any, command: Command, page: str, plan: Any, saved: Any, operation: str,
    write_attempts: int | None = None,
) -> _Verification:
    """Run the live re-read when the decision depends on it: after an applied `set`, and after a
    "No changes detected" answer with requested values (the notification alone proves nothing)."""
    applied = saved is None or saved.status == "applied"
    # An applied LAN address move must not read the old endpoint again (restore already refuses to);
    # the reconnect is the verification, so the result carries the reconnect address unverified.
    moved = saved is not None and (saved.lan_address_changed is True or saved.reconnect_address is not None)
    no_change_to_check = (
        saved is not None
        and observed_no_change(saved.status, saved.write_performed, saved.write_attempted)
        and _requested_values(plan)
    )
    reread = saved is not None and saved.reread_required
    if (applied and (operation == "set" or reread) and not moved) or no_change_to_check:
        return _verify_set(
            client, page, plan, command.options.include_secrets,
            reservations_verified=saved is not None and saved.state_observed is True,
            write_attempts=write_attempts,
        )
    return _Verification()


def _report_save(
    client: Any, command: Command, page: str, plan: Any, saved: Any, operation: str,
    button: str | None = None, *, status_code: int | None = None, location: str | None = None,
    write_attempts: int | None = None,
) -> None:
    """Single reporter for every save path: the outcome -> exit code mapping lives in save_result.
    `write_attempts` is the POST count of a write the transport re-sent after a Login-page answer."""
    # The POST count of a re-sent write joins the step before anything below can raise or word a text.
    counts = [n for n in (write_attempts, getattr(saved, "write_attempts", None)) if n is not None]
    write_attempts = max(counts) if counts else None
    if saved is not None and write_attempts is not None and saved.write_attempts != write_attempts:
        saved = replace(saved, write_attempts=write_attempts)
    try:
        check = _save_verification(client, command, page, plan, saved, operation, write_attempts)
    except RouterAuthError as exc:  # includes RouterSessionPoolFullError (never page-level)
        # The write was sent before this verification read failed: the text says so (and how often).
        message = str(exc)
        if saved is not None:
            exc.write_attempted = saved.write_attempted
            exc.write_evidence = _step_write_evidence(saved)
            if exc.write_evidence.get("committed") and saved.acknowledgement_observed is True:
                message = append_sentence(message, "Changes saved was observed")
            elif observed_no_change(saved.status, saved.write_performed, saved.write_attempted):
                # An observed no-change answer (committed false) before the re-read failed: say so.
                message = append_sentence(message, "No changes detected was observed")
        else:
            # Save-less page: the verification read runs only after the POST was answered, so the write
            # was sent and answered; nothing else (commit, acknowledgement) was observed.
            exc.write_attempted = True
            exc.write_evidence = {**(getattr(exc, "write_evidence", None) or {}), "writeResponseReceived": True}
            _add_write_attempts(exc, write_attempts)
        exc.args = (append_sentence(message, sent_guidance(write_attempts)),)
        raise
    verified, mismatches, attempts = check.verified, check.mismatches, check.attempts
    requested = _requested_values(plan)
    if saved is None:
        # No acknowledgement protocol applies to this page/button: the POST response is the evidence.
        decision = decide_save_result("applied", None, None, verified, mismatches, requested=requested,
                                      verify_warning=check.read_error, write_attempts=write_attempts)
    else:
        decision = decision_for_step(saved, verified, mismatches, requested=requested,
                                     verify_warning=check.read_error)
    if decision.committed:
        if operation == "set":
            result = set_committed(page, status_code, location, verified=verified, mismatches=mismatches,
                                   warning=check.warning)
        else:
            result = submit_committed(page, button, status_code, location, verified=verified,
                                      mismatches=mismatches, warning=check.warning)
        result = replace(result, outcome=decision.outcome, verify_attempts=attempts)
    else:
        result = OperationResult(
            operation=operation, dry_run=False, committed=False, page=page,
            guarded=True, dangerous=page in DANGEROUS_PAGES, button=button,
            status_code=status_code, location=location, outcome=decision.outcome,
            verified=verified, mismatches=mismatches, verify_attempts=attempts,
            warning=(
                None if decision.positive
                else " ".join(t for t in (decision.message, getattr(plan, "warning", None)) if t) or None
            ),
            result=decision.message if decision.positive else None,
        )
    if saved is not None:
        result = _with_save_evidence(result, saved, status_code=status_code, location=location)
    else:
        # The gateway answered the POST (2xx/3xx) and no acknowledgement protocol applies to it.
        result = replace(result, write_attempted=True, write_response_received=True,
                         acknowledgement_observed=False)
    if write_attempts is not None and result.write_attempts is None:
        result = replace(result, write_attempts=write_attempts)
    command.exit_code = decision.exit_code
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


def _with_save_evidence(result: OperationResult, saved: Any, *, status_code: int | None = None,
                        location: str | None = None) -> OperationResult:
    """Preserve the same observed write facts for Wi-Fi, LAN and generic operation output. A step
    without its own status code/location (e.g. a failed Wi-Fi Warning confirmation) keeps the
    caller's observed POST values instead of nulling them."""
    if saved.status_code is not None:
        status_code = saved.status_code
    elif status_code is None:
        # A failed write carries the HTTP status that ended it only as the error's status.
        status_code = getattr(saved, "error_status_code", None)
    return replace(
        result,
        status_code=status_code,
        location=saved.location if saved.location is not None else location,
        reconnect_address=saved.reconnect_address, write_attempted=saved.write_attempted,
        write_response_received=saved.write_response_received,
        # A write that was never sent was not performed either.
        write_performed=False if saved.write_attempted is False and saved.write_performed is None
        else saved.write_performed,
        acknowledgement_observed=saved.acknowledgement_observed,
        write_attempts=getattr(saved, "write_attempts", None),
    )


def _report_wifi_save(client: Any, command: Command, page: str, plan: Any,
                      operation: str, button: str | None = None) -> bool:
    if page not in {"wconfig", "wconfig_unified"} or not any(name.lower() == "save" for name in plan.raw_payload):
        return False
    step = RestoreStep(1, "form", page, "save Wi-Fi settings", raw_payload=plan.raw_payload)
    saved = _presend_failure_step(execute_restore(client, [step]).steps[0])
    # A connection error during the save is a failed step like on every other page (structured
    # result, exit 2); only pool-full and authentication failures keep raising.
    _raise_if_coordination_failure(saved)
    _report_save(client, command, page, plan, saved, operation, button)
    return True


def _report_lan_save(client: Any, command: Command, page: str, plan: Any, parsed: Any,
                     operation: str, button: str | None = None) -> bool:
    if page != "dhcpserver" or not any(name.lower() == "save" for name in plan.raw_payload):
        return False
    live = _live_form_values(parsed)
    if not any(name in plan.display_changes and plan.raw_payload.get(name) != live.get(name)
               for name in ("ipaddr", "ipmask", "dhcp")):
        return False
    step = RestoreStep(1, "form", page, "save LAN settings", raw_payload=plan.raw_payload,
                       reconnect_required=True, reconnect_address=plan.raw_payload.get("ipaddr"),
                       lan_address_changed=("ipaddr" in plan.display_changes
                                            and plan.raw_payload.get("ipaddr") != live.get("ipaddr")),
                       postcondition=RestorePostcondition(form_fields=tuple(plan.display_changes)))
    saved = _presend_failure_step(execute_restore(client, [step]).steps[0])
    _raise_if_coordination_failure(saved)
    saved = _acknowledged_write_as_applied(saved)
    _report_save(client, command, page, plan, saved, operation, button)
    return True


def _acknowledged_write_as_applied(step: Any) -> Any:
    """A step that observed "Changes saved" for a performed write but did not see the requested
    state (the form reads back differently, or the later reads failed) is an applied write whose
    live re-read decides the exit code (1 differs, 2 unreadable). Every save path reports it the
    same way; a rejection banner or a pending LAN move keeps its own step."""
    if (step.status == "failed" and step.acknowledgement_observed is True and step.write_performed is True
            and not step.lan_address_changed
            and not (step.error or "").startswith(WRITE_REJECTED_PREFIX)):
        return replace(step, status="applied", error=None, reread_required=True)
    return step


def _raise_if_coordination_failure(step: Any) -> None:
    """Pool-full and authentication failures keep their own types (and exit 2) on every save path."""
    message = step.error or UNCONFIRMED_TEXT
    if step.session_pool_full:
        waited_ms, retry_count = pool_full_metadata(step)
        pool_full = session_pool_full_error(message, waited_ms=waited_ms, retry_count=retry_count)
        pool_full.write_attempted = step.write_attempted
        pool_full.write_evidence = _step_write_evidence(step)
        raise pool_full
    if step.error_type == "RouterAuthError" and not step.error_page_level:
        # A 401/403 answered by one content page is that page's failure, reported as the step.
        error = RouterAuthError(message)
        if step.error_status_code is not None:
            error.status_code = step.error_status_code
        error.write_attempted = step.write_attempted
        error.write_evidence = _step_write_evidence(step)
        raise error


def _step_write_evidence(step: Any) -> dict[str, Any]:
    """What a step observed of its write, for a failure raised with the step in hand: when the step
    never sent its POST (`write_attempted` is False) the pre-send shape; otherwise the evidence of the
    sent write, where the acknowledgement and the commit survive a later verification fault, pool
    exhaustion included."""
    if step.write_attempted is False:
        return presend_write_evidence()
    acknowledged = step.acknowledgement_observed is True and step.write_performed is not False
    evidence: dict[str, Any] = {
        "writeResponseReceived": step.write_response_received,
        "writePerformed": step.write_performed,
        "acknowledgementObserved": step.acknowledgement_observed,
        "committed": (step.status == "applied" or acknowledged) and step.acknowledgement_observed is True,
    }
    attempts = getattr(step, "write_attempts", None)
    if attempts is not None:
        evidence["writeAttempts"] = attempts
    return {key: value for key, value in evidence.items() if value is not None}


def _post_observed(
    client: Any, page: str, send: Callable[[], Any], *, describe_raised: bool = False,
    evidence: PostWriteEvidence | None = None,
) -> Any:
    """Run one write POST (`send`) under transport write observation. Returns the response, or a
    failed step carrying the write evidence when the POST (or the nonce read before it) answered an
    HTTP error or a page-level 401/403, or the transport failed. Pool-full and session-wide
    authentication failures raise with their type and coordination data; with `describe_raised`
    their message also says whether the write was sent. A caller that needs the transport's count of
    configuration POSTs for a SUCCESSFUL write (a Login-page answer re-sends once) passes its own
    `evidence`: it is complete when this returns."""
    evidence = evidence if evidence is not None else PostWriteEvidence()
    try:
        with observe_post(client, evidence):
            response = send()
            evidence.attempted = evidence.response_received = True
    except (RouterResponseError, RouterConnectionError, RouterAuthError) as exc:
        if isinstance(exc, RouterAuthError) and not is_page_level_auth_error(exc):
            if describe_raised:
                exc.args = (_write_evidence_guidance(str(exc), evidence),)
                exc.write_attempted = evidence.attempted
                if evidence.attempted is False:
                    exc.write_evidence = presend_write_evidence()
                elif evidence.response_received is True:
                    exc.write_evidence = {**(getattr(exc, "write_evidence", None) or {}), "writeResponseReceived": True}
            _add_write_attempts(exc, _resent_attempts(evidence))
            raise
        return _write_failure_step(page, exc, evidence)
    return response


def _post_save(
    client: Any, page: str, payload: dict[str, str], evidence: PostWriteEvidence | None = None
) -> Any:
    """`_post_observed` for a page's own form (authentication and the nonce read happen first)."""
    return _post_observed(
        client, page, lambda: client.post_cgi_page(page, payload), describe_raised=True, evidence=evidence
    )


def _add_write_attempts(exc: BaseException, attempts: int | None) -> None:
    """A raised failure after a re-sent write carries the POST count in its write evidence."""
    if attempts is not None:
        exc.write_evidence = {**(getattr(exc, "write_evidence", None) or {}), "writeAttempts": attempts}


def _resent_attempts(evidence: PostWriteEvidence) -> int | None:
    """The number of write POSTs when the transport re-sent the write (a Login-page answer forces one
    re-login and re-send), None for a single POST."""
    return evidence.attempts if evidence.attempts is not None and evidence.attempts > 1 else None


def _confirm_save(
    client: Any, page: str, payload: dict[str, str], sent: PostWriteEvidence | None = None
) -> tuple[int | None, str | None, Any | None]:
    """POST a page's form and collect the gateway's acknowledgement step. When the gateway answers
    with a Wi-Fi Warning redirect, post Continue to the owning form (without it the change is
    discarded). Returns (status_code, location, step); step is None when no acknowledgement
    protocol applies to the page. Pool-full/authentication failures raise; every other outcome is
    returned for the caller to decide through save_result. A caller that reports `writeAttempts`
    passes its own `sent` evidence, complete when this returns."""
    posted = _post_save(client, page, payload, sent)
    if isinstance(posted, RestoreStepResult):
        # The same "could not answer" the Wi-Fi/LAN paths report as a failed step: structured, exit 2.
        return None, None, posted
    return _confirm_response(
        client, page, payload, posted, attempts=_resent_attempts(sent) if sent is not None else None
    )


def _with_attempts(step: RestoreStepResult, attempts: int | None) -> RestoreStepResult:
    """`step` carrying the POST count of a re-sent write (the larger of the two when it has its own)."""
    if attempts is None or (step.write_attempts or 0) >= attempts:
        return step
    return replace(step, write_attempts=attempts)


def _confirm_response(
    client: Any, page: str, payload: dict[str, str], response: Any, *, read_redirect: bool = True,
    attempts: int | None = None,
) -> tuple[int, str | None, Any | None]:
    """The acknowledgement half of `_confirm_save` for a POST the gateway answered 2xx/3xx.
    `attempts` is the POST count of a write re-sent after a Login-page answer (None for one POST):
    every step and raised failure below carries it."""
    status_code, location = response.status_code, _location(response.headers)
    if is_confirmation_redirect(location):
        step = _with_attempts(
            confirm_warning_page(client, location, status_code=status_code, page=page, write_attempts=attempts),
            attempts,
        )
        _raise_if_coordination_failure(step)
        return step.status_code or status_code, step.location, step
    if page in {"services", "apphosting"} or any(name.lower() == "save" for name in payload):
        step = confirm_saved_page(client, page, response, payload=payload, write_attempts=attempts)
        _raise_if_coordination_failure(step)
        return status_code, location, step
    # A Save-less page answers the write itself: a rejection banner inline in a 200 body, or on
    # the page a 3xx redirects to, is a definitive negative. "No changes detected. Save not
    # performed." (with or without the error icon) is not a rejection: the write was answered and
    # nothing was saved, so it is an `unchanged` step that save_result decides like on Wi-Fi (a
    # re-read compares requested values; with nothing requested it is unchanged, exit 0).
    answered = _answer_step(client, page, response, read_redirect=read_redirect, attempts=attempts)
    if answered is not None:
        return status_code, location, answered
    # The inline answer carries the whole notification: "Changes saved" is an observed acknowledgement.
    if status_code == 200 and save_notification(page, response.body).saved:
        return status_code, location, RestoreStepResult(
            0, page, "form", f"save {page}", "applied", status_code=status_code, location=location,
            write_attempted=True, write_response_received=True, write_performed=True,
            acknowledgement_observed=True, write_attempts=attempts,
        )
    return status_code, location, None


def _answer_step(
    client: Any, page: str, response: Any, *, read_redirect: bool = True, attempts: int | None = None
) -> RestoreStepResult | None:
    """The step for a write the gateway answered with a notification (inline in a 200 body, or on
    the page a 3xx redirects to): a rejection banner is a failed step (exit 1); "No changes detected.
    Save not performed." (with or without the error icon) is an `unchanged` step, decided by
    save_result like the Wi-Fi notification. None when the answer carries neither."""
    status_code, location = response.status_code, _location(response.headers)
    banner = _answer_banner(client, page, response, read_redirect=read_redirect, attempts=attempts)
    if isinstance(banner, RestoreStepResult):
        return banner
    if banner and not no_changes_text(banner):
        return _rejection_step(page, status_code, location, banner, attempts)
    if banner or (status_code == 200 and no_changes_notice(response.body)):
        return RestoreStepResult(
            0, page, "form", f"save {page}", "unchanged", status_code=status_code, location=location,
            write_attempted=True, write_response_received=True, write_performed=False,
            acknowledgement_observed=False, write_attempts=attempts,
        )
    return None


def _answer_banner(
    client: Any, page: str, response: Any, seen: list[str] | None = None, *, read_redirect: bool = True,
    attempts: int | None = None,
) -> str | RestoreStepResult | None:
    """The gateway's rejection banner for an answered write: inline in a 200 body, or on the page a
    3xx answer redirects to. None when the answer carries no banner (or is not 200/3xx). When the
    redirect target cannot be read (HTTP error, page-level 401/403) the write's answer is unknown:
    a failed step carrying the POST's own evidence (exit 2), as is a lost connection on that read.
    A lost session or a full pool raises with the write evidence. `read_redirect=False` skips the
    redirect read (the write took the web server down); an inline body is still examined.
    The redirect target's body is appended to `seen` (the diagnostic keeps it as its first result).
    `attempts` is the POST count of a write re-sent after a Login-page answer: the evidence and the
    failure text of this read say how many times the change was sent."""
    if response.status_code == 200:
        return router_error_banner(response.body)
    if not 300 <= response.status_code < 400 or not read_redirect:
        return None
    location = _location(response.headers)
    sent = PostWriteEvidence(attempted=True, response_received=True, attempts=attempts)
    try:
        target = redirect_answer_page(client, location)
        if target is None:
            return None
        if seen is not None:
            seen.append(target.body)
        rejection = router_error_banner(target.body)
        if rejection is None and no_changes_notice(target.body):
            # The same icon-independent notice the inline body path recognises.
            return f"{NO_CHANGES_TEXT}."
        return rejection
    except RouterAuthError as exc:  # includes RouterSessionPoolFullError (never page-level)
        if not is_page_level_auth_error(exc):
            exc.args = (_write_evidence_guidance(str(exc), sent),)
            exc.write_attempted = True
            if sent.response_received is True:
                exc.write_evidence = {**(getattr(exc, "write_evidence", None) or {}), "writeResponseReceived": True}
            _add_write_attempts(exc, attempts)
            raise
        failure: Exception = exc
    except (RouterResponseError, RouterConnectionError) as exc:
        failure = exc
    return replace(
        _write_failure_step(page, failure, sent),
        status_code=response.status_code, location=location,
        error=_write_evidence_guidance(f"could not read the gateway's answer at {location}: {failure}", sent),
    )


def _rejection_step(
    page: str, status_code: int | None, location: str | None, banner: str, attempts: int | None = None
) -> RestoreStepResult:
    """The gateway answered the write and named the rejection: a definitive negative (exit 1)
    reported as a structured result on every path, actions and diagnostics included."""
    return RestoreStepResult(
        0, page, "form", f"save {page}", "failed", status_code=status_code, location=location,
        error=f"{WRITE_REJECTED_PREFIX}{banner}", write_attempted=True, write_response_received=True,
        acknowledgement_observed=False, write_attempts=attempts,
    )


NO_WRITE_SENT = "The request failed before any write was sent; nothing was changed."


def presend_write_evidence() -> dict[str, Any]:
    """The one evidence shape of a failure before any write POST was sent, for every save path
    (generic, Wi-Fi, LAN move; structured result or raised pool-full/auth error): nothing was sent,
    answered, performed, acknowledged or committed."""
    return {
        "writeAttempted": False, "writeResponseReceived": False, "writePerformed": False,
        "acknowledgementObserved": False, "committed": False,
    }


def _presend_failure_step(step: Any) -> Any:
    """A restore-runner step that failed before any POST carries the same evidence and sentence as a
    generic save's pre-send failure (`presend_write_evidence`, `NO_WRITE_SENT`)."""
    if step.write_attempted is not False or step.status != "failed":
        return step
    error = step.error or UNCONFIRMED_TEXT
    if NO_WRITE_SENT not in error:
        error = append_sentence(error, NO_WRITE_SENT)
    return replace(step, error=error, acknowledgement_observed=False, write_performed=False,
                   write_response_received=False)


def _write_evidence_guidance(message: str, evidence: PostWriteEvidence) -> str:
    """`message` plus what the transport observed: nothing sent, sent and answered, or delivery unknown."""
    if evidence.attempted is False:
        return append_sentence(message, NO_WRITE_SENT)
    if evidence.attempts is not None and evidence.attempts > 1:
        # The one documented re-send after a Login-page answer happened.
        answered = "the last one was answered" if evidence.response_received else "the last one got no answer"
        return append_sentence(
            message,
            f"The change was sent {evidence.attempts} times (a Login-page answer forces one re-login and "
            f"re-send) and {answered}; {VERIFY_BEFORE_RETRYING}",
        )
    return write_guidance(message, {"write_attempted": evidence.attempted}, sent=evidence.response_received is True)


def _write_failure_step(page: str, exc: Exception, evidence: PostWriteEvidence) -> RestoreStepResult:
    """A failed write POST (HTTP error, page-level 401/403 or transport failure) as a failed step
    carrying the transport's write evidence: not sent, sent and answered, or delivery unknown."""
    status = getattr(exc, "status_code", None)
    error = _write_evidence_guidance(str(exc), evidence)
    # The POST's own answer when the transport observed it; otherwise (legacy getters) the error's
    # status stands in only when the POST was answered. A later GET's error is never the POST's status.
    post_status = evidence.status_code if evidence.attempts is not None else status
    return RestoreStepResult(
        0, page, "form", f"save {page}", "failed",
        status_code=post_status if evidence.response_received else None,
        error=error,
        write_attempted=evidence.attempted, write_response_received=evidence.response_received,
        acknowledgement_observed=False,
        error_type=type(exc).__name__,
        error_status_code=status if evidence.attempts is None or post_status is not None else None,
        error_page_level=is_page_level_auth_error(exc), write_attempts=evidence.attempts,
    )


def _post_and_confirm(
    client: Any, page: str, payload: dict[str, str], *, read_redirect: bool = True,
    evidence: PostWriteEvidence | None = None,
) -> tuple[int, str | None] | RestoreStepResult:
    """`_confirm_save` for callers that only need an applied write (actions), which have no plan to
    verify against. Returns (status_code, location) for an applied write. Every other outcome is
    returned as a failed step carrying the decision's message: a failed write POST or an
    unconfirmed outcome (no acknowledgement, a "No changes detected" answer that cannot be verified
    here) means the router state is unknown, and an explicit rejection banner keeps its
    WRITE_REJECTED_PREFIX message (`_report_failed_write` maps it to exit 1). Pool-full and
    session-wide authentication failures raise."""
    posted = _post_save(client, page, payload, evidence)
    if isinstance(posted, RestoreStepResult):
        return posted
    status_code, location, step = _confirm_response(
        client, page, payload, posted, read_redirect=read_redirect,
        attempts=_resent_attempts(evidence) if evidence is not None else None,
    )
    if step is None or step.status == "applied":
        return status_code, location
    # An action requests no field values: "No changes detected" is a complete answer (unchanged).
    decision = decision_for_step(step, requested=False)
    if decision.exit_code == 0:
        return replace(step, status="unchanged", error=None)
    return replace(step, status="failed", error=decision.message)


def _report_failed_write(command: Command, result: OperationResult, step: RestoreStepResult) -> None:
    """A committed action/diagnostic whose write did not apply: structured `failed` result. An
    explicit rejection banner is a definitive negative (exit 1); anything else leaves the router
    state unknown (exit 2)."""
    result = replace(
        _with_save_evidence(result, step), committed=False, outcome="failed", warning=step.error, result=None,
    )
    rejected = (step.error or "").startswith(WRITE_REJECTED_PREFIX)
    command.exit_code = NEGATIVE_ANSWER if rejected else NO_ANSWER
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


class _RecordingGetter:
    """Hands `fetch_parsed_page` the client's GET while keeping the exception it turned into a failed
    result: the retry policy needs the error's type and HTTP status, which the result does not carry."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.error: Exception | None = None
        self.body: str | None = None

    def get_cgi_page(self, page: str, **kwargs: Any) -> Any:
        try:
            response = self._client.get_cgi_page(page, **kwargs)
        except Exception as exc:
            self.error = exc
            raise
        self.body = getattr(response, "body", None)
        return response


def _verification_read(client: Any, page: str) -> tuple[Any | None, str | None, int, str | None]:
    """Read `page` for the post-save check through `fetch_parsed_page` (parse, not-found and login-page
    handling live there), retrying retryable failures with VERIFY_RETRY_DELAYS between attempts:
    the transient HTTP statuses shared with the acknowledgement poll (restore.TRANSIENT_VERIFICATION_STATUSES),
    connection loss, and a lost session (logged in again, forced, before the next attempt; a login that
    cannot reach the gateway or answers a transient status is a failed attempt, a refused login ends
    the retries). Page-level 401/403, other 4xx and unparsable pages are final. Pool-full
    propagates untouched. Returns (parsed or None, last error, attempts, raw body of the successful
    read or None): the parser keeps data rows only, so a table's header row is visible in the raw
    body alone."""
    error: str | None = None
    relogin = False
    attempts = len(VERIFY_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            sleep(VERIFY_RETRY_DELAYS[attempt - 2])
        getter = _RecordingGetter(client)
        logins_before = 0
        try:
            if relogin:
                relogin = False
                try:
                    client.login(force=True)
                except RouterSessionPoolFullError:
                    raise
                except RouterAuthError as exc:
                    # Credentials refused (or the session cannot be re-established): retrying cannot help.
                    return None, _relogin_refused(exc), attempt, None
            # The client counts the logins it performs; a change across the GET alone means it logged
            # in again on its own (a Login-page bounce), so a lost session after that is final.
            logins_before = getattr(client, "login_attempts", 0)
            result = fetch_parsed_page(getter, page, include_secrets=True)
        except RouterSessionPoolFullError:
            raise
        except RouterAuthError as exc:
            # fetch_parsed_page re-raises only a session-wide authentication failure. When the client
            # already logged in again during the GET (a 302 to the Login page) and still failed, the
            # gateway refused the session: final. Otherwise log in again before the next attempt.
            error = str(exc)
            if getattr(client, "login_attempts", 0) != logins_before:
                return None, _relogin_refused(exc), attempt, None
            relogin = True
            continue
        except (RouterResponseError, RouterConnectionError) as exc:
            # The forced login itself could not reach the gateway or answered an HTTP error.
            error = str(exc)
            if isinstance(exc, RouterResponseError) and not is_transient_verification_status(exc.status_code):
                return None, error, attempt, None
            continue
        if result.ok and result.parsed is not None:
            # The same structural checks a write's source page passes: a "Please wait" document, a
            # parser-cut page or a page without any control is unreadable, not a form that lacks the
            # field. The gateway is often still busy right after a save, so it is retried like a
            # transient failure.
            unreadable = truncated_page_problem(page, result.parsed) or missing_form_controls(
                page, result.parsed, any_page=True
            )
            if unreadable is None:
                return result.parsed, None, attempt, getter.body
            error = unreadable
            continue
        error = result.error or "page unavailable"
        failure = getter.error
        retryable = isinstance(failure, RouterConnectionError) or (
            isinstance(failure, RouterResponseError) and is_transient_verification_status(failure.status_code)
        )
        if not retryable:
            return None, error, attempt, None
    return None, error, attempts, None


def _relogin_refused(error: Exception) -> str:
    """The re-read lost the session and the gateway refused logging in again."""
    return f"the session was lost and the re-login was refused: {error}"


def _verify_set(
    client: Any, page: str, plan: Any, include_secrets: bool = False, *, reservations_verified: bool = False,
    write_attempts: int | None = None,
) -> _Verification:
    """Re-read the page after a set commit and compare every requested field with its live value.
    A 302 only means the gateway accepted the POST; a discarded form still answers 302.
    `verified` is None (with `read_error`) when the re-read failed. With `reservations_verified` (the
    save wait observed every `alloc_<mac>` reservation of the POST as a Fixed Allocation row) those
    requested reservations are verified by that state and not compared with the re-read: the gateway
    no longer renders their editor select, so the field would read as absent. Every other
    `alloc_<mac>` assignment is verified from the re-read's Fixed Allocation rows after the
    table-header gate (MAC compared case-insensitively, IP exactly; a non-IP value such as `normal`
    means the device's row must not be a Fixed Allocation), never from the select the gateway stops
    rendering. Any other requested field is still compared; with none left no re-read is made."""
    requested = {k: v for k, v in plan.raw_payload.items() if k in (plan.display_changes or {})}
    plan_warning = getattr(plan, "warning", None) or None
    if reservations_verified and page == "ipalloc":
        requested = {
            name: value for name, value in requested.items()
            if not name.lower().startswith("alloc_")
        }
        if not requested:
            return _Verification(True, None, plan_warning=plan_warning)
    parsed, error, attempts, body = _verification_read(client, page)
    if parsed is None:
        tries = f" after {attempts} attempts" if attempts > 1 else ""
        read_error = append_sentence(
            f"could not re-read {page} to verify the change{tries}: {error or 'page unavailable'}",
            sent_guidance(write_attempts),
        )
        return _Verification(read_error=read_error, plan_warning=plan_warning, attempts=attempts)
    row_mismatches: dict[str, dict[str, str]] = {}
    if page == "ipalloc":
        # A reservation is verified from the table rows, never from the `alloc_<mac>` select the
        # gateway stops rendering once the save went through. An IPv4 value wants a Fixed Allocation
        # row with that address; any other value (`normal`, "address from the DHCP pool") releases the
        # reservation and wants the device not to be a Fixed Allocation row.
        reservation_fields = {name: value for name, value in requested.items() if name.lower().startswith("alloc_")}
        if reservation_fields:
            try:
                # A page without its table header is a failed read, never an empty table; a header-only
                # table (header visible in the raw body, no data rows) is a readable empty section.
                structure = missing_table_structure(page, parsed, body)
                if structure is not None:
                    raise SnapshotExtractionError(structure)
                rows = extract_snapshot({page: parsed}, ts="", router_host="").reservations
            except SnapshotExtractionError as exc:
                read_error = append_sentence(
                    f"could not read the {page} table to verify the change: {exc}", sent_guidance(write_attempts),
                )
                return _Verification(read_error=read_error, plan_warning=plan_warning, attempts=attempts)
            live_ips = {r.mac.lower(): r.ip for r in rows}
            listed_macs = {
                row[key].strip().lower()
                for row in parsed.tables
                if (key := find_column(row, RESERVATION_COLUMNS["mac"])) is not None
            }
            for name, wanted in reservation_fields.items():
                mac = name[len("alloc_"):]
                live_ip = live_ips.get(mac.lower())
                if not IPV4_PATTERN.fullmatch(wanted):
                    release_wanted = f"no Fixed Allocation row for {mac} (address from the DHCP pool)"
                    if live_ip is not None:
                        live_text = f"Fixed Allocation row {mac} -> {live_ip}"
                    elif mac.lower() not in listed_macs:
                        live_text = f"no row for {mac} on the IP Allocation table; the release could not be confirmed"
                    else:
                        continue
                    row_mismatches[name] = {
                        "wanted": redact_value(name, release_wanted, include_secrets),
                        "live": redact_value(name, live_text, include_secrets),
                    }
                elif live_ip != wanted:
                    row_mismatches[name] = {
                        "wanted": redact_value(
                            name, f"Fixed Allocation row {mac} -> {wanted}", include_secrets
                        ),
                        "live": redact_value(
                            name,
                            f"Fixed Allocation row {mac} -> {live_ip}" if live_ip is not None
                            else f"no Fixed Allocation row for {mac}",
                            include_secrets,
                        ),
                    }
            requested = {name: value for name, value in requested.items() if name not in reservation_fields}
    live = _live_form_values(parsed)
    # The plan and the re-read are both parsed with secrets included; a password control is secret
    # by its type, which only the parsed pages know.
    secret = getattr(plan, "sensitive_names", frozenset()) | page_sensitive_names(parsed)
    mismatches = {
        name: {
            "wanted": redact_value(name, wanted, include_secrets, secret),
            "live": redact_value(name, live.get(name, "<absent>"), include_secrets, secret),
        }
        for name, wanted in requested.items()
        if live.get(name) != wanted
    }
    mismatches = {**row_mismatches, **mismatches}
    return _Verification(not mismatches, mismatches or None, plan_warning=plan_warning, attempts=attempts)


def _live_form_values(parsed: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for f in parsed.fields:
        if f.type in ("checkbox", "radio") and not f.checked:
            continue
        values[f.name] = f.value
    for sel in parsed.selects:
        values[sel.name] = sel.value
    for ta in parsed.textareas:
        values[ta.name] = ta.value
    return values


def _run_submit(client: Any, command: Command) -> None:
    page = resolve_cgi_page(_require_arg(command, "page name"))
    if len(command.args) < 2 or not command.args[1]:
        raise UsageError("Missing button name.")
    button = command.args[1]
    assignments = command.args[2:]
    token = confirm_token_for_page(page)
    if command.commit:
        if page in DANGEROUS_PAGES:
            raise UsageError(
                f"Refusing generic submit on dangerous page '{page}'. Use an explicit action command if supported."
            )
        if command.confirm != token:
            raise UsageError(f"Refusing to submit '{button}' on '{page}'. Re-run with --commit --confirm {token}.")

    page_result = _fetch_form_page(client, page)
    if not page_result.ok or page_result.parsed is None:
        _report_fetch_failure(command, page_result)
        return
    plan = build_submit_plan(page, page_result.parsed, button, assignments, command.options.include_secrets)

    if not command.commit:
        result = submit_dry_run(plan, button, token)
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    if _report_wifi_save(client, command, page, plan, "submit", button):
        return
    if _report_lan_save(client, command, page, plan, page_result.parsed, "submit", button):
        return
    sent = PostWriteEvidence()
    status_code, location, saved = _confirm_save(client, page, plan.raw_payload, sent)
    if saved is not None:
        saved = _acknowledged_write_as_applied(saved)
    elif plan.button is not None and is_opener_button(page, plan.button.name):
        result = replace(
            submit_committed(page, button, status_code, location), committed=False, outcome="opened",
            write_attempted=True, write_response_received=True, write_performed=False,
            acknowledgement_observed=False, result=OPENED_TEXT, write_attempts=_resent_attempts(sent),
        )
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return
    _report_save(client, command, page, plan, saved, "submit", button, status_code=status_code,
                 location=location, write_attempts=_resent_attempts(sent))


def _run_dump(client: Any, command: Command) -> None:
    include = resolve_dump_include(command.include)
    if command.out_dir:
        # An unusable --out target is refused before the first router request, not after the page reads.
        preflight_dump_target(command.out_dir)
    parsed, failures = _fetch_snapshot_pages(client, snapshot_pages_for(include))
    if failures:
        _report_fetch_failures(command, failures)
        return
    snapshot = extract_snapshot(
        parsed, **_snapshot_meta(command), all_clients=command.all_clients, include=include
    )
    duplicates = duplicate_service_names(snapshot.services)
    if duplicates:
        # The dump loader refuses a file with two services of one name, so writing it would leave a
        # backup that no later diff, restore or autorestore can read.
        raise SnapshotExtractionError(
            "services: the live services table lists these names more than once: "
            f"{', '.join(sanitize_terminal_text(name, single_line=True) for name in duplicates)}; "
            "nothing was written"
        )
    problem = snapshot_problem(snapshot)
    if problem is not None:
        # The loader's own rule: a file it would refuse is no backup, so nothing is written.
        raise SnapshotExtractionError(
            "the live pages produce a dump that cannot be read back: "
            f"{sanitize_terminal_text(problem, single_line=True)}; nothing was written"
        )
    path = command.out_dir or str(default_dump_path())
    write_dump_file(path, snapshot)
    command.output(fmt.snapshot_summary_output(snapshot, path), lambda: fmt.print_snapshot_summary(snapshot, path))


def _dump_optional_pages(dump: Snapshot) -> tuple[str, ...]:
    """Optional form pages the dump captured: the only optional pages diff/restore fetch and compare."""
    return tuple(page for page in OPTIONAL_FORM_PAGES if page in dump.forms)


def _warn_missing_pages(missing: list[str]) -> None:
    for page in missing:
        sys.stderr.write(
            f"warning: page '{sanitize_terminal_text(page, single_line=True)}' is not present in the dump; "
            "nothing to compare/restore\n"
        )


def _nothing_selected_in_dump(
    command: Command, pages: Sequence[str] | None, missing: list[str], *, status: str | None = None
) -> bool:
    """True after reporting a usage error (exit 1) when `--include` named only pages the dump never
    captured: there is nothing to compare or restore, which is not "No differences." / success.
    The stdout JSON keeps `missingPages` (and carries `status` when the caller names one)."""
    if not pages or any(page not in missing for page in pages):
        return False
    shown = ", ".join(sanitize_terminal_text(page, single_line=True) for page in missing)
    message = f"nothing compared: {shown} not in dump"
    command.exit_code = NEGATIVE_ANSWER
    payload: dict[str, Any] = {"ok": False, "error": message, "missingPages": missing}
    if status is not None:
        payload["status"] = status
    command.output(payload, lambda: sys.stderr.write(f"{message}\n"))
    return True


def _run_diff(client: Any, command: Command) -> None:
    dump = read_dump_file(_require_arg(command, "dump file"))
    pages = resolve_restore_include(command.include)
    missing = pages_missing_from_dump(dump, pages)
    if _nothing_selected_in_dump(command, pages, missing):
        return
    _warn_missing_pages(missing)
    optional = _dump_optional_pages(dump)
    parsed, failures = _fetch_snapshot_pages(client, snapshot_read_pages(dump, pages))
    if failures:
        _report_fetch_failures(command, failures)
        return
    live = extract_snapshot(parsed, **_snapshot_meta(command), include=optional, selected_pages=pages)
    diff = diff_snapshots(dump, live, pages=pages)
    command.exit_code = 0 if diff.identical else 1
    command.output(
        {**fmt.display_diff(diff, command.options.include_secrets), "missingPages": missing},
        lambda: fmt.print_snapshot_diff(diff, include_secrets=command.options.include_secrets),
    )


def _run_restore(client: Any, command: Command) -> dict[str, Any] | None:
    if command.commit and command.confirm != RESTORE_CONFIRM_TOKEN:
        raise UsageError(f"Refusing to restore. Re-run with --commit --confirm {RESTORE_CONFIRM_TOKEN}.")
    dump = read_dump_file(_require_arg(command, "dump file"))
    pages = resolve_restore_include(command.include)
    missing = pages_missing_from_dump(dump, pages)
    if _nothing_selected_in_dump(command, pages, missing):
        return None
    _warn_missing_pages(missing)
    optional = _dump_optional_pages(dump)
    fetch_pages = snapshot_read_pages(dump, pages)
    if command.prune and "services" in fetch_pages and "apphosting" not in fetch_pages:
        # A service remove is planned only once no live forward uses it, so --prune with only
        # `services` selected still reads the forwards page (it is never written or compared).
        fetch_pages = tuple(p for p in SNAPSHOT_PAGES if p in {*fetch_pages, "apphosting"})
    parsed, failures = _fetch_snapshot_pages(client, fetch_pages)
    if failures:
        _report_fetch_failures(command, failures)
        return
    preflight_report = None
    preflight = None
    rescan_evidence = None

    def report_preflight_failure(exc: BgwError) -> dict[str, Any]:
        report = allocation_preflight_report(preflight, error=exc, rescan=rescan_evidence)
        command.exit_code = PAGE_FETCH_FAILED
        coordination = {"sessionPoolFull": isinstance(exc, RouterSessionPoolFullError)}
        if isinstance(exc, RouterSessionPoolFullError):
            waited_ms, retry_count = pool_full_metadata(exc)
            coordination.update(waitedMs=waited_ms, retryCount=retry_count)
        command.output(
            {"ok": False, "steps": [], "diff": None, "allocationPreflight": report,
             "planStatus": "blocked", "planComplete": False, "missingPages": missing, **coordination},
            lambda: sys.stdout.write(f"{_stdout_line(report['reason'])}\n"),
        )
        return coordination

    pending = pending_allocation_requests(dump, parsed, pages)
    if pending:
        preflight_coordination = {"sessionPoolFull": False}
        try:
            preflight = inspect_allocation_conflicts(
                client, pending,
                **({"snapshot_pages": parsed} if isinstance(parsed, OwnershipSnapshotPages) else {}),
            )
        except BgwError as exc:
            if command.commit:
                # Same structured "blocked" report the later rescan/plan stages return: exit 2 with
                # allocationPreflight JSON, not a bare exit 1 that reads as "the restore drifted".
                return report_preflight_failure(exc)
            if isinstance(exc, RouterSessionPoolFullError):
                waited_ms, retry_count = pool_full_metadata(exc)
                preflight_coordination = {"sessionPoolFull": True, "waitedMs": waited_ms, "retryCount": retry_count}
            preflight_report = allocation_preflight_report(error=exc)
        else:
            if preflight.conflicts:
                preflight_report = allocation_preflight_report(preflight)
        if preflight_report is not None:
            if not command.commit:
                operation = replace(restore_dry_run([], RESTORE_CONFIRM_TOKEN), result=preflight_report["reason"])

                def print_preflight() -> None:
                    for conflict in preflight_report["conflicts"]:
                        sys.stdout.write(
                            f"allocation conflict: {_stdout_line(conflict['ip'])} is held by "
                            f"{_stdout_line(conflict['holderMac'])}; wanted for {_stdout_line(conflict['targetMac'])}\n"
                        )
                    sys.stdout.write(f"{_stdout_line(preflight_report['reason'])}\n")
                    fmt.print_operation(operation)

                if preflight_report["planStatus"] == "blocked":
                    command.exit_code = PAGE_FETCH_FAILED
                command.output(
                    {"steps": [], "allocationPreflight": preflight_report,
                     "planStatus": preflight_report["planStatus"], "planComplete": False,
                     **preflight_coordination,
                     "operation": fmt.operation_output(operation), "missingPages": missing},
                    print_preflight,
                )
                return preflight_coordination
            rescan_evidence = AllocationRescanEvidence()
            try:
                rescan_allocation_conflicts(
                    client, preflight, evidence=rescan_evidence,
                    log=(lambda _line: None) if command.options.json
                    else (lambda line: sys.stdout.write(f"{_stdout_line(line)}\n")),
                )
                preflight_report = allocation_preflight_report(preflight, rescan=rescan_evidence)
                # Clear changes device identities and positional controls. Never use the old plan inputs.
                parsed, failures = _fetch_snapshot_pages(client, fetch_pages)
                if failures:
                    failed_pages = ", ".join(f.page for f in failures)
                    raise RouterConnectionError(
                        f"Device rescan completed but restore pages could not be refreshed: {failed_pages}"
                    )
            except BgwError as exc:
                return report_preflight_failure(exc)
    include_secrets = command.options.include_secrets
    restore_options = RestoreOptions(prune=command.prune, include_secrets=include_secrets, pages=pages)
    try:
        live = extract_snapshot(parsed, **_snapshot_meta(command), include=optional, selected_pages=pages)
        diff = diff_snapshots(dump, live, pages=pages)
        steps = build_restore_plan(diff, dump, parsed, restore_options)
    except BgwError as exc:
        if preflight_report is None:
            raise
        return report_preflight_failure(exc)
    if preflight_report is not None:
        preflight_report.update(
            planStatus="complete", planComplete=True,
            reason="Allocation ownership verified; restore plan built from refreshed pages.",
        )

    if not command.commit:
        plan = restore_dry_run(steps, RESTORE_CONFIRM_TOKEN)

        def print_plan() -> None:
            fmt.print_restore_plan(steps)
            fmt.print_operation(plan)

        command.output(
            {
                "steps": fmt.display_restore_steps(steps, include_secrets),
                "operation": fmt.operation_output(plan),
                "missingPages": missing,
            },
            print_plan,
        )
        return

    on_step = None if command.options.json else fmt.print_restore_step_result
    execution = execute_restore(client, steps, on_step)
    result = restore_committed(execution)
    after_diff = None
    after_failures = []
    verification_error = None
    pool_step = next((s for s in execution.steps if s.session_pool_full), None)
    coordination = {}
    if pool_step is not None:
        waited_ms, retry_count = pool_full_metadata(pool_step)
        coordination = {"sessionPoolFull": True, "waitedMs": waited_ms, "retryCount": retry_count}
    reconnect = next(
        (s for s in execution.steps if s.status == "reconnect-required" or s.lan_address_changed), None
    )
    terminal = next(
        (
            s for s in execution.steps
            if s.session_pool_full or (s.error_type == "RouterAuthError" and not s.error_page_level)
        ),
        None,
    )
    try:
        if reconnect is not None:
            raise RouterConnectionError(reconnect_verification_reason(reconnect))
        if terminal is not None:
            # A full session pool or a lost session ended the run: no further router read, the
            # coordinator records the cooldown from the result.
            skipped = f"Post-restore verification skipped: {terminal.error or 'the gateway session was lost'}"
            if terminal.session_pool_full:
                waited_ms, retry_count = pool_full_metadata(terminal)
                raise session_pool_full_error(skipped, waited_ms=waited_ms, retry_count=retry_count)
            raise RouterAuthError(skipped)
        after_parsed, after_failures = _fetch_snapshot_pages(client, fetch_pages)
        if not after_failures:
            after_diff = diff_snapshots(
                dump,
                extract_snapshot(after_parsed, **_snapshot_meta(command), include=optional, selected_pages=pages),
                pages=pages,
            )
    except BgwError as exc:
        verification_error = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, RouterSessionPoolFullError):
            waited_ms, retry_count = pool_full_metadata(exc)
            coordination.update(sessionPoolFull=True, waitedMs=waited_ms, retryCount=retry_count)
            verification_error.update(waitedMs=waited_ms, retryCount=retry_count)
    converged = execution.stopped_at is None and after_diff is not None and restore_converged(after_diff, command.prune)
    # A write the gateway never answered keeps the run at "no answer" (exit 2): a readable closing
    # diff cannot say whether that write landed, so it never downgrades the run to "differs" (exit 1).
    unanswered = unanswered_write_step(execution.steps)
    # A step that failed on an HTTP/transport/session fault, even before any write was sent, is no
    # answer: only an explicit rejection or a known non-convergence is a negative answer (exit 1).
    fault = next((s for s in execution.steps if s.status == "failed" and s.error_type is not None), None)
    command.exit_code = (
        NO_ANSWER if after_diff is None or unanswered is not None or fault is not None else (0 if converged else 1)
    )

    def print_result() -> None:
        fmt.print_operation(result)
        if unanswered is not None:
            sys.stdout.write(
                f"Step {unanswered.order} ({_stdout_line(unanswered.page)}) had no answer from the gateway; "
                "its outcome is unknown (exit 2).\n"
            )
        if after_diff is not None:
            fmt.print_snapshot_diff(after_diff, include_secrets=include_secrets)
            return
        if verification_error is not None:
            sys.stdout.write(f"Post-restore verification unavailable: {_stdout_line(verification_error['message'])}\n")
            return
        # The router was written to; never let the pages that could not be re-read disappear.
        sys.stdout.write("Post-restore verification incomplete; these pages could not be re-fetched:\n")
        for failure in after_failures:
            fmt.print_page_fetch_error(failure)

    command.output(
        {
            "execution": fmt.execution_output(execution),
            "diff": fmt.display_diff(after_diff, include_secrets) if after_diff is not None else None,
            "verificationFailures": fmt.summarize_fetch_failures(after_failures),
            **({"verificationError": verification_error} if verification_error is not None else {}),
            "writeUnanswered": unanswered is not None,
            **({"executionFault": {"order": fault.order, "page": fault.page, "error": fault.error}}
               if fault is not None else {}),
            **({"unansweredWrite": {"order": unanswered.order, "page": unanswered.page, "error": unanswered.error}}
               if unanswered is not None else {}),
            **coordination,
            "operation": fmt.operation_output(result),
            "missingPages": missing,
            **({"allocationPreflight": preflight_report} if preflight_report is not None else {}),
        },
        print_result,
    )
    return coordination


def _run_autorestore(client: Any, command: Command) -> dict[str, Any] | None:
    """Timer-driven factory-reset recovery (see autorestore.py). Login with fallback lives here: when
    the primary access code is rejected and BGW_FALLBACK_ACCESS_CODE is set, the client is rebuilt
    with the fallback and login retried once; using it alone is no reset signal (it only corroborates a
    reset-shaped diff or an unfinished recovery, and the result warns to re-set the access code). A cached primary
    session that the first page read finds refused is dropped and the same login is redone, so a dead
    cache never keeps the fallback code from being tried."""
    if command.commit and command.confirm != RESTORE_CONFIRM_TOKEN:
        raise UsageError(f"Refusing to autorestore. Re-run with --commit --confirm {RESTORE_CONFIRM_TOKEN}.")
    dump = read_dump_file(_require_arg(command, "dump file"))
    pages = resolve_restore_include(command.include)
    missing = pages_missing_from_dump(dump, pages)
    if _nothing_selected_in_dump(command, pages, missing, status="nothing-selected"):
        # Before login and any checkpoint: with nothing selected there is no reset to recognise.
        return None
    _warn_missing_pages(missing)
    json_mode = command.options.json
    fallback = os.environ.get(FALLBACK_ACCESS_CODE_ENV) or None
    primary = command.access_code or getattr(getattr(client, "options", None), "access_code", None)
    fallback_clients: list[Any] = []
    command.evidence_clients = fallback_clients  # an interrupt reads their POST evidence too
    # Empty until the first login, then whether the primary client entered it holding the imported cached
    # session (its login() is then a no-op, so a dead cache only shows on the first page read).
    cached_session: list[bool] = []

    def login_with_fallback() -> tuple[Any, bool]:
        if not cached_session:
            cached_session.append(client.has_authenticated_session())
        try:
            client.login()
            return client, False
        except RouterSessionPoolFullError:
            raise
        except RouterAuthError:
            # Only a rejected primary code can mean "the router reverted to the sticker code"; a
            # missing primary or an identical fallback would just fail the same way again.
            if not fallback or not primary or fallback == primary:
                raise
            on_session_wait = None if json_mode else _print_session_wait
            fallback_client = _client_factory(
                command.options, fallback, on_session_wait=on_session_wait, user_agent=USER_AGENT
            )
            fallback_clients.append(fallback_client)
            fallback_client.login()
            return fallback_client, True

    relogged: list[bool] = []

    def relogin_after_rejected_cache(error: RouterAuthError) -> tuple[Any, bool] | None:
        # The cached primary session was refused on the first read and the primary code refused on
        # the forced re-login: after a factory reset that is the sticker code's job. Drop the dead
        # session and log in exactly as an uncached run would (primary, then the fallback). Once
        # only, and never for a pool-full wait, one forbidden page or a session made this run.
        if relogged or fallback_clients or not cached_session or not cached_session[0]:
            return None
        if isinstance(error, RouterSessionPoolFullError) or is_page_level_auth_error(error):
            return None
        if not fallback or not primary or fallback == primary:
            return None
        relogged.append(True)
        client.clear_session()
        return login_with_fallback()

    # A closed stdout (`autorestore --commit | head -1`) must never end the run before its intent and
    # failure bookkeeping: the first BrokenPipeError (and only that) silences every later write.
    output_closed: list[bool] = []

    def close_output() -> None:
        output_closed.append(True)
        # What is still buffered would fail again when the interpreter flushes at exit (status 120).
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - lives to the end of the process

    def guarded_write(write: Callable[[], Any]) -> None:
        if output_closed:
            return
        try:
            write()
        except BrokenPipeError:
            close_output()

    def log(line: str) -> None:
        def write() -> None:
            sys.stdout.write(f"{_stdout_line(line)}\n")
            # Each progress line reaches the journal as it is written (stdout is a pipe under the
            # service manager): a kill at the unit's start timeout loses at most the current line.
            sys.stdout.flush()

        if not json_mode:
            guarded_write(write)

    def on_step(step: Any) -> None:
        if not output_closed and not fmt.print_restore_step_result(step):
            close_output()

    try:
        result = run_autorestore(
            login_with_fallback,
            dump,
            AutorestoreOptions(
                commit=command.commit,
                max_passes=command.max_passes,
                wait_seconds=command.wait_seconds,
                on_any_diff=command.on_any_diff,
                pages=pages,
            ),
            fetch_pages=_fetch_snapshot_pages,
            checkpoint=RecoveryCheckpoint(command.options.host, dump, pages),
            log=log,
            on_step=None if json_mode else on_step,
            relogin=relogin_after_rejected_cache,
            **_snapshot_meta(command),
        )
    finally:
        # The session coordinator wraps the primary client. When the fallback code logged in, its
        # session is the live one: hand it to the primary client so the coordinator caches it (and
        # replaces the primary's dead cached session) instead of the next run logging in twice.
        if fallback_clients and fallback_clients[-1].has_authenticated_session():
            client.import_session(fallback_clients[-1].export_session())
    command.exit_code = result.exit_code

    def print_result() -> None:
        if result.status == "restore-needed" and result.plan is not None:
            fmt.print_restore_plan(result.plan)
        final = result.final_diff
        # A healthy timer run is the single `no-reset: no differences` log line in the journal; a
        # no-reset run with drift still prints that drift.
        if result.status == "no-reset" and final is not None and final.identical and not final.firmware_changed:
            return
        if final is not None:
            fmt.print_snapshot_diff(final)

    output = {**result_output(result), "missingPages": missing}
    guarded_write(lambda: (command.output(output, print_result), sys.stdout.flush()))
    return output


def _run_fixture_capture(client: Any, command: Command) -> None:
    """Hidden port of scripts/capture-router-fixtures.ts: sanitized fixture pack under --out (tests/fixtures)."""
    # The access code was already resolved once when the client was built; resolving again would
    # re-read an exhausted stdin under --access-code-stdin and wrongly refuse.
    if not (command.access_code or getattr(getattr(client, "options", None), "access_code", None)):
        raise UsageError("Set BGW_ACCESS_CODE to capture authenticated router fixtures.")
    fixture_root = Path(command.out_dir) if command.out_dir else Path.cwd() / "tests" / "fixtures"
    preflight_fixture_root(fixture_root)
    pages = sweep_router(
        client,
        SweepOptions(
            delay_ms=max(750, command.delay_ms),
            pages=command.pages,
            include_raw=True,
            include_parsed=True,
            include_secrets=False,
            use_fallbacks=False,
        ),
    )
    pool_full = next((page for page in pages if page.session_pool_full), None)
    if pool_full is not None:
        # Same rule as _run_sweep: a page skipped for a full session pool must abort the capture
        # so the coordinator records the cooldown and the command exits 2.
        waited_ms, retry_count = pool_full_metadata(pool_full)
        raise session_pool_full_error(waited_ms=waited_ms, retry_count=retry_count)
    degraded: list[str] = []
    if not command.options.json:
        if capture_fixture_pack(pages, fixture_root, degraded=degraded) == 0 and pages:
            command.exit_code = NO_ANSWER  # every fetch failed: only failure receipts were written
        return
    # JSON mode: progress lines go to stderr; stdout carries one JSON summary.
    captured = capture_fixture_pack(pages, fixture_root, stdout=sys.stderr, degraded=degraded)
    if captured == 0 and pages:
        command.exit_code = NO_ANSWER
    fmt.print_json({
        "out": str(fixture_root),
        "captured": captured,
        "total": len(pages),
        "degraded": degraded,
        "pages": [
            {
                "page": page.page,
                "ok": page.ok,
                "captured": bool(page.raw_html) and page.page not in degraded,
                "degraded": page.page in degraded,
                "error": page.error,
            }
            for page in pages
        ],
    })


# ---------------------------------------------------------------------------------------------
# shared helpers


def _stdout_line(text: object) -> str:
    """Router-derived text as one terminal-safe line for default (non --json, non --raw) stdout."""
    return sanitize_terminal_text(str(text), single_line=True)


def _print_page(client: Any, command: Command, page: str, *, forms: bool) -> None:
    from .status import fetch_device_status, fetch_home_network_status, fetch_security_options

    include_secrets = command.options.include_secrets
    limit = command.limit
    if not command.raw:
        if page == "home":
            status = fetch_device_status(client, include_secrets)
            _flag_unanswered(command, status)
            command.output(status, lambda: fmt.print_device_status(status, limit=limit))
            return
        if page == "lanstatistics":
            status = fetch_home_network_status(client, include_secrets)
            _flag_unanswered(command, status)
            command.output(status, lambda: fmt.print_composite_status("Home Network Status", status, limit=limit))
            return
        if page == "securityoptions":
            status = fetch_security_options(client, include_secrets)
            _flag_unanswered(command, status)
            command.output(status, lambda: fmt.print_composite_status("Security Options", status, limit=limit))
            return

    if command.raw:
        response = _fetch_raw_page(client, command, page)
        if response is None or _unavailable_answer(command, page, response):
            return
        body = response.body
        # JSON callers get one JSON object on stdout for every outcome, the raw HTML included.
        command.output({"page": page, "raw": body}, lambda: sys.stdout.write(body))
        return

    if page == "devices":
        result = fetch_device_list(client, include_offline=command.all)
        if result.unread:
            command.exit_code = NO_ANSWER  # neither devices.ha nor the ipalloc fallback was read
        command.output(result, lambda: fmt.print_device_list(result, limit=limit))
        return

    if page == "logs":
        response = _fetch_raw_page(client, command, page)
        if response is None:
            return
        # An HTTP 200 "Page not found" or Login document is an unavailable page, not an empty log.
        if _unavailable_answer(command, page, response):
            return
        # --limit shapes the text view only; --json always carries every entry.
        logs = parse_logs(response.body)
        if logs is None:
            # No table with the log columns: the page did not show its log, which is not an empty one.
            _report_fetch_failure(
                command,
                ParsedPageResult(page, False, response.status_code, error="the page shows no log table"),
            )
            return
        command.output(logs, lambda: fmt.print_logs(logs, limit=limit))
        return

    result = fetch_parsed_page(client, page, include_secrets=include_secrets)
    if not result.ok or result.parsed is None:
        _report_fetch_failure(command, result)
        return
    parsed = result.parsed
    if parsed.truncated:
        sys.stderr.write(f"note: {truncated_page_problem(page, parsed)}; the output is incomplete\n")
    command.output(fmt.parsed_page_output(parsed), lambda: fmt.print_parsed_page(parsed, forms=forms, limit=limit))


def _flag_unanswered(command: Command, status: Any) -> None:
    from .status import status_answered

    if not status_answered(status):
        command.exit_code = NO_ANSWER  # the primary page and every fallback page were unreadable


def _fetch_raw_page(client: Any, command: Command, page: str) -> Any | None:
    try:
        return client.get_cgi_page(page)
    except RouterAuthError as error:  # includes RouterSessionPoolFullError (a subclass)
        if not is_page_level_auth_error(error):
            raise
        # One page answering 401/403 is reported like any unavailable page (exit 2), not an abort.
        _report_fetch_failure(command, ParsedPageResult(page, False, error=str(error)))
        return None
    except Exception as error:  # noqa: BLE001 - per-page failures are reported (exit 2), not raised
        _report_fetch_failure(command, ParsedPageResult(page, False, error=str(error) or error.__class__.__name__))
        return None


def _run_sweep(client: Any, command: Command, *, force_compact: bool = False) -> list[Any]:
    limited = command.pages
    if command.raw and not command.out_dir and (not limited or len(limited) != 1):
        raise UsageError(
            "Refusing to emit raw HTML for a full sweep. Use --pages <page> with exactly one page, or use --out <dir>."
        )

    write_artifacts = command.out_dir is not None
    if write_artifacts:
        # An unusable --out tree is refused before the 37-page walk, not after it.
        preflight_sweep_output(command.out_dir or "")
    include_parsed = not force_compact and (
        command.include_parsed or command.full or command.name == "schema" or write_artifacts
    )
    include_forms = not force_compact and (command.forms or command.name == "schema")
    include_raw = not force_compact and (command.raw or write_artifacts)

    def progress(event: SweepProgressEvent) -> None:
        if event.phase == "start":
            sys.stderr.write(f"sweep {event.index}/{event.total} {event.page} start\n")
        else:
            sys.stderr.write(f"sweep {event.index}/{event.total} {event.page} {event.status or 'failed'}\n")

    pages = sweep_router(
        client,
        SweepOptions(
            delay_ms=command.delay_ms,
            pages=limited,
            include_parsed=include_parsed,
            include_forms=include_forms,
            include_raw=include_raw,
            include_secrets=command.options.include_secrets,
            use_fallbacks=not include_raw,
            on_page_progress=None if command.options.json else progress,
        ),
    )

    pool_full = next((page for page in pages if page.session_pool_full), None)
    if pool_full is not None:
        waited_ms, retry_count = pool_full_metadata(pool_full)
        raise session_pool_full_error(waited_ms=waited_ms, retry_count=retry_count)

    # A sweep in which no page loaded is no answer (exit 2); the per-page errors are still printed.
    command.exit_code = sweep_exit_code(pages)

    if command.raw and not write_artifacts and not command.options.json:
        if pages and pages[0].ok:
            sys.stdout.write(pages[0].raw_html or "")
        if pages and not pages[0].ok:
            # stdout carries only the raw HTML, so a page that could not be read says why on stderr.
            reason = fmt.sanitize_terminal_text(pages[0].error or "page unavailable")
            sys.stderr.write(f"error: page {pages[0].page} could not be read: {reason}\n")
        return []

    if write_artifacts:
        return write_sweep_artifacts(pages, command.out_dir or "")

    if include_parsed or include_forms or include_raw:
        return pages
    return [strip_large_payloads(page) for page in pages]


def _fetch_snapshot_pages(
    client: Any, pages: Sequence[str]
) -> tuple[dict[str, ParsedPage], list[ParsedPageResult]]:
    parsed = OwnershipSnapshotPages()
    failures: list[ParsedPageResult] = []
    for page in pages:
        reader = _BodyRecorder(parsed.allocation_reader(client) if page == "ipalloc" else client)
        result = fetch_parsed_page(reader, page, include_secrets=True)
        unreadable = None
        if result.ok and result.parsed is not None:
            unreadable = (
                truncated_page_problem(page, result.parsed)
                or missing_table_structure(page, result.parsed, reader.body)
                or missing_form_controls(page, result.parsed)
            )
        if unreadable is not None:
            # A table section without its header row or the gateway's empty-table cell, or a form page
            # without any control, is unreadable, never an empty section (see snapshot._TABLE_SECTIONS).
            failures.append(
                ParsedPageResult(page, False, result.status_code, error=unreadable, structural=True)
            )
        elif result.ok and result.parsed is not None:
            parsed[page] = result.parsed
        elif page in BEST_EFFORT_PAGES:
            sys.stderr.write(
                f"warning: documentary page '{page}' could not be read "
                f"({sanitize_terminal_text(result.error or 'page unavailable', single_line=True)}); "
                "continuing without it\n"
            )
        else:
            failures.append(result)
    return parsed, failures


class _BodyRecorder:
    """Hands `fetch_parsed_page` the page reader while keeping the body it answered, so the snapshot
    reader can check the table structure the parsed rows alone cannot show."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self.body: str | None = None

    def get_cgi_page(self, page: str, **kwargs: Any) -> Any:
        response = self._reader.get_cgi_page(page, **kwargs)
        self.body = getattr(response, "body", None)
        return response


def _snapshot_meta(command: Command) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return {"ts": ts, "router_host": command.options.host}


def _report_fetch_failure(command: Command, result: ParsedPageResult) -> None:
    # The parsed page of a failed read (parsed with secrets for the dump file) never reaches stdout.
    result = replace(result, parsed=None)
    command.exit_code = PAGE_FETCH_FAILED
    command.output(result, lambda: fmt.print_page_fetch_error(result))


def _report_fetch_failures(command: Command, failures: list[ParsedPageResult]) -> None:
    # Snapshot pages are parsed with include_secrets so the dump file (0600) is complete; the failure
    # report must not carry those parsed pages into stdout.
    command.exit_code = PAGE_FETCH_FAILED

    def print_failures() -> None:
        for failure in failures:
            fmt.print_page_fetch_error(failure)

    command.output({"ok": False, "failures": fmt.summarize_fetch_failures(failures)}, print_failures)


def _location(headers: Any) -> str | None:
    if not headers:
        return None
    for key, value in dict(headers).items():
        if str(key).lower() == "location":
            if isinstance(value, list | tuple):
                value = value[0] if value else ""
            return str(value or "") or None
    return None


def _require_arg(command: Command, name: str) -> str:
    if not command.args or not command.args[0]:
        raise UsageError(f"Missing {name}.")
    return command.args[0]


# ---------------------------------------------------------------------------------------------
# argument parsing


class _Parser(argparse.ArgumentParser):
    """argparse that reports usage problems like the TS CLI: message on stderr, exit 1 (not 2)."""

    def error(self, message: str) -> Any:  # type: ignore[override]
        raise UsageError(message)


def _numeric(value: str, name: str, minimum: int) -> int:
    try:
        parsed = float(value)
    except ValueError:
        parsed = float("nan")
    if parsed != parsed or parsed in (float("inf"), float("-inf")) or parsed < minimum:
        raise UsageError(f"{name} must be a finite number greater than or equal to {minimum}.")
    return int(parsed)


_TIMEOUT_MAX_SECONDS = Decimal(3600)


def _timeout_milliseconds(value: str) -> int:
    """Convert CLI seconds once, retaining millisecond precision without float truncation."""
    try:
        seconds = Decimal(value)
        if not seconds.is_finite() or seconds < Decimal("0.001"):
            raise ValueError
        sign, digits, exponent = seconds.as_tuple()
        # Shift the decimal exponent exactly; arithmetic would round using the ambient context.
        milliseconds = Decimal((sign, digits, exponent + 3))
        if not math.isfinite(float(milliseconds)):
            raise ValueError
    except (DecimalException, ValueError, OverflowError) as exc:
        raise UsageError(
            "--timeout must be a finite number of seconds greater than or equal to 0.001 "
            "with a finite millisecond equivalent."
        ) from exc
    if seconds > _TIMEOUT_MAX_SECONDS:
        # --timeout took milliseconds before the seconds migration; an unmigrated `--timeout 15000`
        # would otherwise be accepted as 15000 s and hang for hours against a dead gateway.
        message = f"--timeout is in seconds and must be at most {_TIMEOUT_MAX_SECONDS}; got {value}."
        if seconds >= 1000:
            suggested = (seconds / Decimal(1000)).normalize()
            message += f" If this is a millisecond value from an older command line, use --timeout {suggested:f}."
        raise UsageError(message)
    return int(milliseconds)


_FLAG = "store_true"
# (flags, kwargs) for every global and per-command option; help strings mirror the TS printHelp text.
_GLOBAL_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("--host",), {"metavar": "<host>", "help": "Router host. Default: BGW_HOST, ROUTER_IP, or 192.168.1.254"}),
    (("--access-code-stdin",), {"action": _FLAG, "help": "Read the device access code from stdin"}),
    (("--json",), {"action": _FLAG, "help": "Print script-friendly JSON"}),
    (("--include-secrets",), {"action": _FLAG, "help": "Include sensitive local output instead of redacting it"}),
    (
        ("--timeout",),
        {
            "metavar": "<seconds>",
            "help": "Request timeout in seconds, at most 3600. Default: 15 (home/lanstatistics: 45 unless set)",
        },
    ),
    (("--strict-tls",), {"action": _FLAG, "help": "Enforce TLS validation"}),
    (("--wait-for-session",), {"action": _FLAG, "help": "Wait/retry when the router says all web sessions are in use"}),
    (("--session-wait-timeout",), {"metavar": "<ms>", "help": "Wait timeout. Default: 120000"}),
    (("--session-wait-interval",), {"metavar": "<ms>", "help": "Wait poll interval. Default: 10000"}),
)
_COMMAND_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("--raw",), {"action": _FLAG, "help": "Emit router HTML instead of parsed output"}),
    (("--forms",), {"action": _FLAG, "help": "Include form controls in terminal output"}),
    (("--all",), {"action": _FLAG, "help": "devices: include offline devices"}),
    (("--limit",), {"metavar": "<n>", "help": "Limit displayed rows. Default: 20"}),
    (("--commit",), {"action": _FLAG, "help": "Actually POST (operations are dry-run by default)"}),
    (("--confirm",), {"metavar": "<token>", "help": "Required token for every committed operation"}),
    (("--ipv4",), {"dest": "protocol", "action": "store_const", "const": "IPv4", "help": "diagnostics: prefer IPv4"}),
    (("--ipv6",), {"dest": "protocol", "action": "store_const", "const": "IPv6", "help": "diagnostics: prefer IPv6"}),
    (("--delay",), {"metavar": "<ms>", "help": "Delay between audit/scan/sweep requests. Default: 750"}),
    (("--pages",), {"metavar": "<csv>", "help": "Limit sweep/scan/schema/audit traversal to selected pages"}),
    (("--include-parsed",), {"action": _FLAG, "help": "Include full parsed page data in sweep/schema JSON"}),
    (("--full",), {"action": _FLAG, "help": "Alias for --include-parsed"}),
    (("--out",), {"dest": "out_dir", "metavar": "<dir>",
                  "help": "Sweep/scan/schema artifact dir; for dump the output FILE path (other commands refuse it)"}),
    (("--prune",), {"action": _FLAG, "help": "Let restore remove router services/forwards missing from the dump"}),
    (
        ("--all-clients",),
        {
            "action": _FLAG,
            "help": "dump: keep every client row in the documentary IP Allocation table (default: Fixed only)",
        },
    ),
    (
        ("--include",),
        {
            "metavar": "<csv|all>",
            "help": "dump: optional pages to capture besides the core; diff/restore: pages to compare/restore",
        },
    ),
    (("--max-passes",), {"metavar": "<n>", "help": "autorestore: restore passes before giving up. Default: 3"}),
    (("--wait",), {"metavar": "<seconds>", "help": "autorestore: pause between passes. Default: 120"}),
    (
        ("--on-any-diff",),
        {"action": _FLAG, "help": "autorestore: treat any restorable difference as a reset (not only total loss)"},
    ),
)


def _common_options() -> argparse.ArgumentParser:
    """Every global and per-command flag; attached to the root and each subparser so flags go anywhere.

    Defaults are SUPPRESS so a subparser never clobbers a value parsed before the command name.
    """
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    for title, table in (("global options", _GLOBAL_OPTIONS), ("command options", _COMMAND_OPTIONS)):
        group = common.add_argument_group(title)
        for flags, kwargs in table:
            group.add_argument(*flags, default=argparse.SUPPRESS, **kwargs)
    return common


_SUBCOMMANDS: tuple[tuple[str, str], ...] = (
    ("check", "Verify the router is reachable."),
    ("auth", "Verify the access code and session login flow."),
    ("sitemap", "Print the live router sitemap."),
    ("coverage", "Compare the CLI tab map against the live router sitemap."),
    ("tabs", "Print the CLI's router tab map."),
    ("actions", "List guarded router actions and confirmation tokens."),
    ("session", "session [status|clear-cache]: inspect or clear local session coordination state."),
    ("action", "action <name>: dry-run a guarded router action; --commit --confirm TOKEN to POST."),
    ("sweep", "Traverse every mapped tab; compact status/count metadata by default."),
    ("scan", "Compatibility alias for compact sweep metadata."),
    ("schema", "Sweep with parsed/form detail enabled."),
    ("audit", "Sweep-backed health check across every mapped tab."),
    ("readiness", "Alias for audit."),
    ("section", "section <section>: print mapped tabs for one router section."),
    ("device", "device [tab]: Device section pages (status, device-list, system-information, ...)."),
    ("broadband", "broadband [tab]: status | configure | fiber-status."),
    ("home-network", "home-network [tab]: status | configure | ipv6 | wi-fi | advanced-wi-fi | ..."),
    ("voice", "voice [tab]: status | line-details | call-statistics."),
    ("firewall", "firewall [tab]: status | packet-filter | nat-gaming | ... | security-options."),
    ("diagnostics", "diagnostics [tab] | diagnostics ping|traceroute|nslookup <host>."),
    ("page", "page <page-or-tab>: fetch and parse any mapped tab or raw CGI page ID."),
    ("inspect", "inspect <page-or-tab>: same as page --forms."),
    ("devices", "List connected devices."),
    ("wifi", "Unified Wi-Fi status/configuration (secrets redacted)."),
    ("nat", "NAT table."),
    ("logs", "Router logs."),
    ("set", "set <page> KEY=VALUE...: dry-run config mutation; --commit --confirm TOKEN to POST."),
    ("submit", "submit <page> <button> [KEY=VALUE...]: dry-run a form/button submit."),
    ("status", "Core status pages."),
    ("dump", "dump [--out <file>] [--include <csv|all>]: capture a configuration snapshot to an owner-only JSON file."),
    ("diff", "diff <dumpfile> [--include <csv|all>]: compare a dump with the live router."),
    ("restore", "restore <dumpfile> [--prune] [--include <csv|all>] [--commit --confirm RESTORE]."),
    (
        "autorestore",
        "autorestore <dumpfile> [--commit --confirm RESTORE] [--max-passes N] [--wait S] [--on-any-diff]: "
        "restore the dump only after a detected factory reset (systemd timer).",
    ),
    ("help", "Show the full help text."),
    ("fixtures-capture", argparse.SUPPRESS),
)


def build_parser() -> _Parser:
    common = _common_options()
    # allow_abbrev=False on every parser: an abbreviated long option (--time, --comm) is rejected
    # instead of silently resolving to a flag the user may not have meant.
    parser = _Parser(prog="bgwcli", parents=[common], add_help=False, allow_abbrev=False)
    parser.add_argument("-h", "--help", action=_FLAG, dest="help_flag", default=argparse.SUPPRESS, help="Show help")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, description in _SUBCOMMANDS:
        sub = subparsers.add_parser(
            name, help=description, description=description, parents=[common], aliases=SECTION_COMMANDS.get(name, ()),
            allow_abbrev=False,
        )
        sub.add_argument("args", nargs="*", help="positional arguments for the command")
    return parser


_ALIAS_TO_COMMAND = {alias: name for name, aliases in SECTION_COMMANDS.items() for alias in aliases}


# Commands that write files under --out (sweep/scan/schema: artifact directory; dump: the dump file;
# fixtures-capture: the fixture pack root).
_OUT_COMMANDS: tuple[str, ...] = ("sweep", "scan", "schema", "dump", "fixtures-capture")


def parse_args(argv: list[str]) -> Command:
    parser = build_parser()
    namespace, leftover = parser.parse_known_args(argv)
    stray_flags = [token for token in leftover if token.startswith("-")]
    if stray_flags:
        raise UsageError(f"unrecognized arguments: {' '.join(stray_flags)}")

    name = getattr(namespace, "command", None)
    if getattr(namespace, "help_flag", False) or name is None:
        return Command(name="help", args=[], options=env_default_options(host=DEFAULT_HOST))
    name = _ALIAS_TO_COMMAND.get(name, name)
    args = [*getattr(namespace, "args", []), *leftover]

    def opt(key: str, default: Any = None) -> Any:
        return getattr(namespace, key, default)

    explicit_host = opt("host")
    if explicit_host is not None and not explicit_host.strip():
        # An explicitly supplied host is used as given: an empty one is never swapped for BGW_HOST or the default,
        # and this check runs before the environment is read.
        raise UsageError(
            "--host is empty: give the router host, or leave --host out to use BGW_HOST, ROUTER_IP or the default"
        )
    # The host variables are read only when --host is absent and the command contacts a router: an explicit
    # host never consults them, and help (which contacts nothing) carries the default instead.
    if explicit_host is None and name == "help":
        explicit_host = DEFAULT_HOST
    options = env_default_options(host=explicit_host)
    if opt("access_code_stdin", False):
        options.access_code_stdin = True
    if opt("json", False):
        options.json = True
    if opt("include_secrets", False):
        options.include_secrets = True
    if opt("timeout") is not None:
        options.timeout_ms = _timeout_milliseconds(opt("timeout"))
        options.timeout_explicit = True
    if opt("strict_tls", False):
        options.insecure_tls = False
    if opt("wait_for_session", False):
        options.wait_for_session = True
    if opt("session_wait_timeout") is not None:
        options.session_wait_timeout_ms = _numeric(opt("session_wait_timeout"), "--session-wait-timeout", 0)
    if opt("session_wait_interval") is not None:
        options.session_wait_interval_ms = _numeric(opt("session_wait_interval"), "--session-wait-interval", 1)

    # A given selection naming no page is a usage error: an empty list must never widen to every page.
    pages = None
    if opt("pages") is not None:
        pages = [page.strip() for page in opt("pages").split(",") if page.strip()]
        if not pages:
            raise UsageError("--pages needs at least one page id.")
    include = None
    if opt("include") is not None:
        include = [page.strip() for page in opt("include").split(",") if page.strip()]
        if not include:
            raise UsageError("--include needs at least one page id (or all).")
    # --out names a destination only the commands that write files use; anywhere else it would be
    # silently ignored (or, for audit, leave empty artifact directories behind).
    if opt("out_dir") is not None and name not in _OUT_COMMANDS:
        raise UsageError(
            f"--out is not used by {name}; it applies to {', '.join(_OUT_COMMANDS[:-1])} and {_OUT_COMMANDS[-1]}."
        )
    if opt("out_dir") is not None and not opt("out_dir").strip():
        raise UsageError(f"--out needs {'a file' if name == 'dump' else 'a directory'}, not an empty value.")

    return Command(
        name=name,
        args=args,
        options=options,
        raw=opt("raw", False),
        forms=opt("forms", False),
        all=opt("all", False),
        commit=opt("commit", False),
        full=opt("full", False),
        confirm=opt("confirm"),
        protocol=opt("protocol"),
        delay_ms=_numeric(opt("delay"), "--delay", 0) if opt("delay") is not None else 750,
        limit=_numeric(opt("limit"), "--limit", 0) if opt("limit") is not None else 20,
        include_parsed=opt("include_parsed", False),
        pages=pages,
        out_dir=opt("out_dir"),
        prune=opt("prune", False),
        all_clients=opt("all_clients", False),
        include=include,
        max_passes=_numeric(opt("max_passes"), "--max-passes", 1) if opt("max_passes") is not None else 3,
        wait_seconds=_numeric(opt("wait"), "--wait", 0) if opt("wait") is not None else 120,
        on_any_diff=opt("on_any_diff", False),
    )


# ---------------------------------------------------------------------------------------------
# help


def help_text() -> str:
    commands = ", ".join(name for name in COMMAND_NAMES if name not in ("help", "fixtures-capture"))
    return f"""bgwcli <command> [args] [options]

Commands: {commands}

Auth:
  Export BGW_ACCESS_CODE once per shell session (the usual way), or pass --access-code-stdin
  to read the code from standard input in scripts that must not export it.
  Secrets are redacted by default. Use --include-secrets only for intentional local inspection.

Most-used read commands:
  bgwcli check | auth
    Verify the router is reachable, or verify the access code and login flow.

  bgwcli device status
    Concise gateway overview. If home.ha hangs, falls back to System Information,
    Broadband Status, and Firewall Status without touching flaky LAN/Fiber pages.

  bgwcli devices [--all] [--limit 20]
    Connected device list. Falls back to IP Allocation if devices.ha hangs.

  bgwcli wifi [--forms] [--json]
    Unified Wi-Fi status/configuration. Passwords/keys stay redacted by default.

  bgwcli nat | logs [--limit 20] | status
    NAT table, router logs, and the core status pages.

  bgwcli broadband status | configure | fiber-status
    Broadband link details, editable broadband config, or optical/fiber module data.

  bgwcli home-network status | configure | ipv6 | wi-fi | advanced-wi-fi | mac-filtering | subnets-dhcp | ip-allocation
    LAN, IPv6, basic/advanced Wi-Fi, MAC filtering, DHCP/subnet, and IP allocation surfaces.

  bgwcli voice status | line-details | call-statistics
    Router-reported POTS/SIP line state, provisioned line details, and complete call-statistic tables.

  bgwcli firewall status | packet-filter | nat-gaming | public-subnet-hosts | ip-passthrough
  bgwcli firewall firewall-advanced | security-options
    Firewall status/config pages. security-options falls back to Firewall Status
    plus Firewall Advanced on firmware where securityoptions.ha returns Page not found.

  bgwcli diagnostics troubleshoot | speed-test | logs | update | resets | syslog | event-notifications | nat-table
    Diagnostics pages, speed-test history, logs, firmware/update/reset/syslog/event/NAT views.

Generic inspection:
  bgwcli page <page-or-tab> [--forms] [--raw] [--json]
    Fetch any tab or CGI page. Use --raw for router HTML.

  bgwcli inspect <page-or-tab> [--json]
    Same as page --forms: includes preserved values, fields, options, textareas,
    buttons, forms, links, disabled/readonly state, and submit targets.

  bgwcli tabs | section <section> | sitemap | coverage
    Show the local tab map, one section, live sitemap, or sitemap-vs-CLI coverage.

  bgwcli session status | clear-cache
    Inspect or clear local session coordination state. clear-cache does not call
    an unverified router logout endpoint, but it also removes the local cooldown.
    Use it only for explicit local-state troubleshooting, not as a retry shortcut.

  bgwcli sweep [--json] [--pages diag,dhcpserver] [--include-parsed] [--forms] [--out router-dumps/latest]
    Shared traversal command for every mapped tab. Default output is compact
    status/count metadata, including duplicate value-entry and link counts.
    Full parsed payloads, form details, raw HTML, and artifact writing are opt-in.

  bgwcli scan [--json]
    Compatibility alias for compact sweep metadata.

  bgwcli schema [--json]
    Sweep with parsed/form detail enabled.

  bgwcli audit [--json] [--delay 750]
    Sweep-backed health check across every mapped tab. Keeps going through hangs
    and reports failed/fallback/empty/useful pages. bgwcli readiness is an alias.

Operations are dry-run by default:
  bgwcli actions
    List explicit guarded actions and their confirmation tokens.

  bgwcli action <name>
    Show the payload and required commit command. Does not POST.
    Example: bgwcli action run-speed-test

  bgwcli action <name> --commit --confirm TOKEN
    POST an explicit action only after the confirmation token matches.
    Examples:
      bgwcli action run-speed-test --commit --confirm SPEED
      bgwcli action restart --commit --confirm RESTART
      bgwcli action restart-wifi-2.4 --commit --confirm RESTART-WIFI   (also restart-wifi-5, restart-broadband)
      bgwcli action find-best-channel-5 --commit --confirm CHANSCAN     (5 GHz channel scan; 5 GHz clients drop briefly)

  bgwcli set <page> KEY=VALUE...
    Build a dry-run config mutation from current form state plus overrides.
    Every commit requires --commit --confirm TOKEN.

  bgwcli submit <page> <button> [KEY=VALUE...]
    Dry-run a discovered router button/form submit. Generic submit is blocked on
    dangerous pages such as restart/reset/update/access-code.

Diagnostics operations:
  bgwcli diagnostics ping <host> [--ipv4|--ipv6]
  bgwcli diagnostics traceroute <host> [--ipv4|--ipv6]
  bgwcli diagnostics nslookup <host> [--ipv4|--ipv6]
    Dry-run diagnostic form submissions.

  bgwcli diagnostics ping <host> --commit --confirm DIAG
    Actually send the diagnostic request. traceroute/nslookup use the same DIAG token.

Backup:
  bgwcli dump [--out <file>] [--include <csv|all>] [--all-clients]
    Capture services, NAT/Gaming forwards, host reservations and the core form
    pages Firewall Advanced (dosprotect) and Advanced Wi-Fi (wconfig) to an
    owner-only JSON file. For dump, --out is the output FILE path.
    --include adds optional form pages: etherlan (LAN ports), dhcpserver
    (Subnets & DHCP), ippass (IP Passthrough), wmacauth (Wi-Fi MAC filtering
    modes); `bgwcli dump --include all` captures every page and is the
    recommended baseline for factory-reset recovery. Pages not included are
    not fetched. The documentary IP Allocation table keeps only Fixed
    Allocation rows by default; --all-clients keeps every client row.

  bgwcli diff <dumpfile> [--include <csv|all>]
    Compare a dump with the live router. Exit 0 identical, 1 different, 2 error.
    Every section and form page present in the dump is compared; --include
    restricts that to the listed page ids (services, apphosting, ipalloc,
    dosprotect, wconfig, etherlan, dhcpserver, ippass, wmacauth). A requested
    page the dump never captured prints a warning on stderr and is skipped.

  bgwcli restore <dumpfile> [--prune] [--include <csv|all>]
    Dry-run the plan that replays missing services/forwards/reservations and
    changed forms, in order: services -> forwards -> reservations -> firewall
    advanced -> Advanced Wi-Fi -> Wi-Fi MAC filtering -> IP Passthrough -> LAN
    ports -> Subnets & DHCP (last: a LAN address change moves the gateway and
    the step carries a warning). --include restricts the plan like diff; a
    requested page missing from the dump becomes a skip step.

  bgwcli restore <dumpfile> --commit --confirm RESTORE
    Apply the plan, then re-diff. Exit 0 once everything in the dump is present
    on the router (router-only extras are left alone unless --prune is given).
    Note: reservations are never released by --prune, only added or corrected.
    Optional pages are only restored when the dump captured them. Removes are
    deferred on any page that still has an add to make, so adds and prunes may
    need two runs. A step shown as applied means the gateway acknowledged the
    save (Changes saved) and the requested values or table state read back; the
    closing diff is the authoritative convergence check.

Automatic recovery (for a systemd timer):
  bgwcli autorestore <dumpfile> [--max-passes 3] [--wait 120] [--on-any-diff] [--include <csv|all>]
    Diff the live router against the dump and decide whether it was factory-reset:
    only when EVERY dumped service, forward and reservation is missing (a dump
    without those sections: every dumped form page differs). Ordinary drift, a
    single removed entry or an edited value, is reported as no-reset and left
    alone. Dry-run by default: prints the plan and exits 1 when a restore is
    needed, 0 when not. --on-any-diff treats any restorable difference as a reset.
    Set BGW_FALLBACK_ACCESS_CODE to the sticker code: when the primary code is
    rejected the login is retried once with it; needing it alone is no reset
    signal (it only corroborates a reset-shaped diff). An unreachable router exits 0 with one line (timers stay quiet).

  bgwcli autorestore <dumpfile> --commit --confirm RESTORE
    Run restore passes (never --prune) until the closing diff converges, sleeping
    --wait seconds between passes, at most --max-passes times. Exit 0 converged,
    1 not converged (the next timer run retries), 2 error: the allocation
    preflight could not complete, pages could not be re-read after a write, the
    recovery checkpoint could not be read or written, another unfinished
    recovery is recorded, or the same failure ended three consecutive runs
    (later runs send nothing until the router converges or the recovery intent
    is removed); check by hand. See deploy/README.md.

Global options:
  --host <host>             Router host. Default: BGW_HOST, ROUTER_IP, or 192.168.1.254
  --access-code-stdin       Read the device access code from stdin
  --json                    Print script-friendly JSON
  --include-secrets         Include sensitive local output instead of redacting it
  --include-parsed          Include full parsed page data in sweep/schema JSON
  --pages <csv>             Limit sweep/scan/schema/audit traversal to selected pages
  --out <dir>               Write sweep raw HTML and parsed JSON artifacts to disk. For dump it is a file path
  --prune                   Let restore remove router services/forwards missing from the dump
                            (deferred on pages that still have an add pending)
  --include <csv|all>       dump: optional pages to capture besides the core; diff/restore: pages to compare/restore
  --max-passes <n>          autorestore: restore passes before giving up. Default: 3
  --wait <seconds>          autorestore: pause between passes. Default: 120
  --on-any-diff             autorestore: treat any restorable difference as a reset, not only total loss
  --timeout <seconds>       Request timeout in seconds, at most 3600. Default: 15 (home/lanstatistics: 45 unless set)
  --delay <ms>              Delay between audit/scan/sweep requests. Default: 750
  --limit <n>               Limit displayed rows. Default: 20
  --wait-for-session        Wait/retry when the router says all web sessions are in use
  --session-wait-timeout <ms>   Wait timeout. Default: 120000
  --session-wait-interval <ms>  Wait poll interval. Default: 10000
  --confirm <token>         Required token for every committed operation
  --strict-tls              Enforce TLS validation. Default router access does not verify its self-signed identity.
  Global options are accepted before or after the command name. <command> --help shows argparse usage.

Notes:
  Read commands only perform GET requests plus the login POST required by the router.
  The router web UI is flaky. Fallbacks are intentionally narrow so normal commands stay fast.
  Session-pool waiting is opt-in so basic commands do not appear hung. Env:
  BGW_WAIT_FOR_SESSION=1, BGW_SESSION_WAIT_TIMEOUT_MS, BGW_SESSION_WAIT_INTERVAL_MS.
  BGW_TIMEOUT_MS remains in milliseconds; --timeout uses seconds (e.g. 1.5).
  An active local pool cooldown always fails fast, including with --wait-for-session.
  Agent bursts reuse a short-lived local router session cache under a per-host
  lock to avoid filling the web session pool. Env:
  BGW_SESSION_CACHE_TTL_MS, BGW_SESSION_POOL_COOLDOWN_MS, BGW_SESSION_LOCK_TIMEOUT_MS.
  JSON dry-runs include operation, dryRun, committed, page, guarded, dangerous,
  confirmation, commitCommand, payload, and changes/result fields where applicable.

Sections: {", ".join(list_sections())}
"""
