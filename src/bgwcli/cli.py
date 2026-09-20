"""Command-line entry point: argparse subcommands, option resolution, dispatch and exit codes.

Port of BGW320-CLI src/cli.ts. Kept deliberately thin: parse -> env defaults + flags -> build the
client -> run the command (inside the session coordinator unless it is purely local) -> render via
format.* or --json -> exit code. Exit codes follow exit_codes.py: 0 ok, 1 negative answer (diff
differs, restore not converged, usage/refusals), 2 could-not-answer (auth, connection, pool full,
dump file, snapshot extraction, page unavailable).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import format as fmt
from .actions import ROUTER_ACTIONS, display_action_payload, get_action
from .audit import build_audit, capture_fixture_pack
from .client import BGW320Client, pool_full_metadata, session_pool_full_error
from .config import GlobalOptions, env_default_options, resolve_access_code
from .devices import fetch_device_list
from .diagnostics import (
    DIAG_CONFIRM_TOKEN,
    build_diagnostic_plan,
    diagnostic_button,
    extract_diagnostic_result,
    is_diagnostic_kind,
    poll_diagnostic_result,
)
from .dumpfile import default_dump_path, read_dump_file, write_dump_file
from .errors import RouterAuthError, RouterSessionPoolFullError, UsageError
from .exit_codes import fatal_exit_code
from .fetch import ParsedPageResult, fetch_parsed_page
from .mutations import DANGEROUS_PAGES, build_mutation_plan, build_submit_plan, confirm_token_for_page
from .operations import (
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
from .pages import list_sections, resolve_page, resolve_section_command, router_tabs, tabs_for_section
from .parser import parse_logs, parse_page, parse_sitemap
from .restore import (
    RestoreOptions,
    build_restore_plan,
    confirm_warning_page,
    execute_restore,
    is_confirmation_redirect,
    restore_converged,
)
from .session import (
    SessionCoordinatorOptions,
    clear_session_state,
    read_session_state,
    router_session_identity,
    with_router_session,
)
from .snapshot import SNAPSHOT_PAGES, extract_snapshot
from .snapshot_diff import diff_snapshots
from .sweep import SweepOptions, SweepProgressEvent, strip_large_payloads, sweep_router, write_sweep_artifacts
from .types import ParsedPage

USER_AGENT = "bgw/0.1.0"
FIXTURE_USER_AGENT = "bgw-fixture-capture/0.1.0"
RESTORE_CONFIRM_TOKEN = "RESTORE"

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
    "set", "submit", "status", "dump", "diff", "restore", "help", "fixtures-capture",
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
    include_lan: bool = False
    exit_code: int = field(default=0, compare=False)

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
                "error": "Router web session pool is full.",
                "sessionPoolFull": True,
                "waitedMs": waited_ms,
                "retryCount": retry_count,
            })
        else:
            sys.stderr.write(f"{error}\n")
        return 2
    except Exception as error:  # noqa: BLE001 - every error is reported through fatal_exit_code (TS parity)
        # Exit 2 keeps `diff` honest: exit 1 means "the router differs from the dump", never "the dump
        # could not be read" or "the live pages could not be turned into a snapshot".
        sys.stderr.write(f"{error}\n")
        return fatal_exit_code(error)


def run(command: Command, client: Any | None = None) -> int:
    """Build the client (unless injected) and run the command, inside the session coordinator when needed."""
    if client is None:
        access_code = resolve_access_code(command.options)
        command.access_code = access_code
        on_session_wait = None if command.options.json else _print_session_wait
        client = _client_factory(command.options, access_code, on_session_wait=on_session_wait, user_agent=USER_AGENT)

    if not uses_session_coordinator(command.name):
        run_command(client, command)
        return command.exit_code

    coordinator = SessionCoordinatorOptions.from_options(command.options)
    with_router_session(client, coordinator, lambda: run_command(client, command))
    return command.exit_code


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


def run_command(client: Any, command: Command) -> None:  # noqa: C901 - one flat switch like cli.ts
    name = command.name
    opts = command.options

    if name == "check":
        result = client.check()
        command.output(result, lambda: fmt.print_check_result(result["host"], result["reachable"], result["title"]))
        return

    if name == "auth":
        client.login()
        command.output({"authenticated": True}, lambda: sys.stdout.write("authenticated\n"))
        return

    if name == "sitemap":
        entries = parse_sitemap(client.get_cgi_page("sitemap", auth=False).body)
        command.output(entries, lambda: fmt.print_sitemap(entries))
        return

    if name == "coverage":
        live_pages = [entry.page for entry in parse_sitemap(client.get_cgi_page("sitemap", auth=False).body)]
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
        page = resolve_page(" ".join(command.args) or _require_arg(command, "page name"))
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
        from .status import fetch_status_sections

        sections = fetch_status_sections(client, opts.include_secrets)
        command.output(sections, lambda: fmt.print_status_sections(sections))
        return

    if name == "dump":
        _run_dump(client, command)
        return

    if name == "diff":
        _run_diff(client, command)
        return

    if name == "restore":
        _run_restore(client, command)
        return

    raise UsageError(f"Unknown command: {name}\nRun bgwcli help.")


_DIRECT_PAGES = {"devices": "devices", "wifi": "wconfig_unified", "nat": "nattable", "logs": "logs"}


# ---------------------------------------------------------------------------------------------
# command bodies


def _run_session(command: Command) -> None:
    action = command.args[0] if command.args else "status"
    origin = router_session_identity(command.options.host)
    if action == "status":
        state = read_session_state(origin)
        command.output(state, lambda: fmt.print_session_state(state))
        return
    if action == "clear-cache":
        clear_session_state(origin, command.options.session_lock_timeout_ms)
        command.output({"ok": True, "cleared": True}, lambda: sys.stdout.write("session cache cleared\n"))
        return
    raise UsageError(f"Unknown session command: {action}")


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

    if action.form_button:
        # The button lives inside the page's main form: post the live base payload + the button.
        page_result = fetch_parsed_page(client, action.page, include_secrets=True)
        if not page_result.ok or page_result.parsed is None:
            _report_fetch_failure(command, page_result)
            return
        plan = build_submit_plan(
            action.page, page_result.parsed, action.form_button, [], command.options.include_secrets
        )
        if plan.blocked:
            raise UsageError(plan.reason or "Action blocked.")
        status_code, location = _post_and_confirm(client, action.page, plan.raw_payload)
    elif action.post_path:
        response = client.post_form(action.page, action.post_path, action.payload)
        status_code, location = response.status_code, _location(response.headers)
    else:
        response = client.post_cgi_page(action.page, action.payload)
        status_code, location = response.status_code, _location(response.headers)
    result = action_committed(action, status_code, location)
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


def _run_diagnostic(client: Any, command: Command) -> None:
    kind = command.args[0]
    if len(command.args) < 2 or not command.args[1]:
        raise UsageError(f"Missing target for diagnostics {kind}.")
    target = command.args[1]
    if command.commit and command.confirm != DIAG_CONFIRM_TOKEN:
        raise UsageError(f"Refusing to run diagnostic '{kind}'. Re-run with --commit --confirm {DIAG_CONFIRM_TOKEN}.")

    diag_page = fetch_parsed_page(client, "diag", include_secrets=True)
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

    response = client.post_cgi_page("diag", plan.raw_payload)
    result_page = parse_page("diag", response.body, include_secrets=command.options.include_secrets)
    text = extract_diagnostic_result(result_page)
    if not text:
        # The gateway runs diagnostics asynchronously: the POST answers 302 with an empty body and the
        # ProgressWindow fills a few seconds later. Poll the page until the output is present and stable.
        text, polled = poll_diagnostic_result(client, include_secrets=command.options.include_secrets)
        if polled is not None:
            result_page = polled
    operation = diagnostic_committed(
        kind, target, response.status_code,
        text
        or (
            f"Router returned status {response.status_code}; no progress output found. "
            "Run 'bgwcli page diag' to read it later."
        ),
    )
    command.output(
        {**fmt.operation_output(operation), "pageResult": fmt.parsed_page_output(result_page)},
        lambda: fmt.print_operation(operation),
    )


def _run_set(client: Any, command: Command) -> None:
    page = resolve_page(_require_arg(command, "page name"))
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

    page_result = fetch_parsed_page(client, page, include_secrets=True)
    if not page_result.ok or page_result.parsed is None:
        _report_fetch_failure(command, page_result)
        return
    plan = build_mutation_plan(page, page_result.parsed, assignments, command.options.include_secrets)
    if plan.blocked:
        raise UsageError(plan.reason or "Mutation blocked.")

    if not command.commit:
        result = set_dry_run(plan, confirmation)
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    status_code, location = _post_and_confirm(client, page, plan.raw_payload)
    verified, mismatches, warning = _verify_set(client, page, plan)
    result = set_committed(page, status_code, location, verified=verified, mismatches=mismatches, warning=warning)
    if verified is False:
        command.exit_code = 1
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


def _post_and_confirm(client: Any, page: str, payload: dict[str, str]) -> tuple[int, str | None]:
    """POST a page's form and, when the gateway answers with a Wi-Fi Warning redirect, post Continue
    to the owning form (without it the change is discarded). Returns (status_code, location)."""
    response = client.post_cgi_page(page, payload)
    status_code, location = response.status_code, _location(response.headers)
    if is_confirmation_redirect(location):
        step = confirm_warning_page(client, location)
        if step.status != "applied":
            raise UsageError(step.error or "Wi-Fi Warning confirmation failed; change not applied.")
        status_code, location = step.status_code or status_code, step.location
    return status_code, location


def _verify_set(client: Any, page: str, plan: Any) -> tuple[bool | None, dict[str, dict[str, str]] | None, str | None]:
    """Re-read the page after a set commit and compare every requested field with its live value.
    A 302 only means the gateway accepted the POST; a discarded form still answers 302."""
    from .mutations import parse_assignments  # noqa: F401 - documents the source of requested values

    requested = {k: v for k, v in plan.raw_payload.items() if k in (plan.display_changes or {})}
    check = fetch_parsed_page(client, page, include_secrets=True)
    if not check.ok or check.parsed is None:
        return None, None, f"could not re-read {page} to verify the change: {check.error or 'page unavailable'}"
    live = _live_form_values(check.parsed)
    mismatches = {
        name: {"wanted": wanted, "live": live.get(name, "<absent>")}
        for name, wanted in requested.items()
        if live.get(name) != wanted
    }
    return (not mismatches), (mismatches or None), (plan.warning if getattr(plan, "warning", None) else None)


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
    page = resolve_page(_require_arg(command, "page name"))
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

    page_result = fetch_parsed_page(client, page, include_secrets=True)
    if not page_result.ok or page_result.parsed is None:
        _report_fetch_failure(command, page_result)
        return
    plan = build_submit_plan(page, page_result.parsed, button, assignments, command.options.include_secrets)

    if not command.commit:
        result = submit_dry_run(plan, button, token)
        command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))
        return

    status_code, location = _post_and_confirm(client, page, plan.raw_payload)
    result = submit_committed(page, button, status_code, location)
    command.output(fmt.operation_output(result), lambda: fmt.print_operation(result))


def _run_dump(client: Any, command: Command) -> None:
    parsed, failures = _fetch_snapshot_pages(client)
    if failures:
        _report_fetch_failures(command, failures)
        return
    snapshot = extract_snapshot(parsed, **_snapshot_meta(command), all_clients=command.all_clients)
    path = command.out_dir or str(default_dump_path())
    write_dump_file(path, snapshot)
    command.output(fmt.snapshot_summary_output(snapshot, path), lambda: fmt.print_snapshot_summary(snapshot, path))


def _run_diff(client: Any, command: Command) -> None:
    dump = read_dump_file(_require_arg(command, "dump file"))
    parsed, failures = _fetch_snapshot_pages(client)
    if failures:
        _report_fetch_failures(command, failures)
        return
    live = extract_snapshot(parsed, **_snapshot_meta(command))
    diff = diff_snapshots(dump, live, include_lan=command.include_lan)
    command.exit_code = 0 if diff.identical else 1
    command.output(fmt.display_diff(diff, command.options.include_secrets), lambda: fmt.print_snapshot_diff(diff))


def _run_restore(client: Any, command: Command) -> None:
    if command.commit and command.confirm != RESTORE_CONFIRM_TOKEN:
        raise UsageError(f"Refusing to restore. Re-run with --commit --confirm {RESTORE_CONFIRM_TOKEN}.")
    dump = read_dump_file(_require_arg(command, "dump file"))
    parsed, failures = _fetch_snapshot_pages(client)
    if failures:
        _report_fetch_failures(command, failures)
        return
    include_secrets = command.options.include_secrets
    restore_options = RestoreOptions(
        prune=command.prune, include_lan=command.include_lan, include_secrets=include_secrets
    )
    live = extract_snapshot(parsed, **_snapshot_meta(command))
    diff = diff_snapshots(dump, live, include_lan=command.include_lan)
    steps = build_restore_plan(diff, dump, parsed, restore_options)

    if not command.commit:
        plan = restore_dry_run(steps, RESTORE_CONFIRM_TOKEN)

        def print_plan() -> None:
            fmt.print_restore_plan(steps)
            fmt.print_operation(plan)

        command.output(
            {"steps": fmt.display_restore_steps(steps, include_secrets), "operation": fmt.operation_output(plan)},
            print_plan,
        )
        return

    on_step = None if command.options.json else fmt.print_restore_step_result
    execution = execute_restore(client, steps, on_step)
    after_parsed, after_failures = _fetch_snapshot_pages(client)
    after_diff = (
        diff_snapshots(dump, extract_snapshot(after_parsed, **_snapshot_meta(command)), include_lan=command.include_lan)
        if not after_failures
        else None
    )
    result = restore_committed(execution)
    converged = execution.stopped_at is None and after_diff is not None and restore_converged(after_diff, command.prune)
    command.exit_code = 0 if converged else 1

    def print_result() -> None:
        fmt.print_operation(result)
        if after_diff is not None:
            fmt.print_snapshot_diff(after_diff)
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
            "operation": fmt.operation_output(result),
        },
        print_result,
    )


def _run_fixture_capture(client: Any, command: Command) -> None:
    """Hidden port of scripts/capture-router-fixtures.ts: sanitized fixture pack under --out (tests/fixtures)."""
    # The access code was already resolved once when the client was built; resolving again would
    # re-read an exhausted stdin under --access-code-stdin and wrongly refuse.
    if not (command.access_code or getattr(getattr(client, "options", None), "access_code", None)):
        raise UsageError("Set BGW_ACCESS_CODE to capture authenticated router fixtures.")
    fixture_root = Path(command.out_dir) if command.out_dir else Path.cwd() / "tests" / "fixtures"
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
        raise session_pool_full_error(waited_ms=pool_full.waited_ms or 0, retry_count=pool_full.retry_count or 0)
    capture_fixture_pack(pages, fixture_root)


# ---------------------------------------------------------------------------------------------
# shared helpers


def _print_page(client: Any, command: Command, page: str, *, forms: bool) -> None:
    from .status import fetch_device_status, fetch_home_network_status, fetch_security_options

    include_secrets = command.options.include_secrets
    limit = command.limit
    if not command.raw:
        if page == "home":
            status = fetch_device_status(client, include_secrets)
            command.output(status, lambda: fmt.print_device_status(status, limit=limit))
            return
        if page == "lanstatistics":
            status = fetch_home_network_status(client, include_secrets)
            command.output(status, lambda: fmt.print_composite_status("Home Network Status", status, limit=limit))
            return
        if page == "securityoptions":
            status = fetch_security_options(client, include_secrets)
            command.output(status, lambda: fmt.print_composite_status("Security Options", status, limit=limit))
            return

    if command.raw:
        sys.stdout.write(client.get_cgi_page(page).body)
        return

    if page == "devices":
        result = fetch_device_list(client, include_offline=command.all)
        command.output(result, lambda: fmt.print_device_list(result, limit=limit))
        return

    if page == "logs":
        response = _fetch_raw_page(client, command, page)
        if response is None:
            return
        logs = parse_logs(response.body)[:limit]
        command.output(logs, lambda: fmt.print_logs(logs))
        return

    result = fetch_parsed_page(client, page, include_secrets=include_secrets)
    if not result.ok or result.parsed is None:
        _report_fetch_failure(command, result)
        return
    parsed = result.parsed
    command.output(fmt.parsed_page_output(parsed), lambda: fmt.print_parsed_page(parsed, forms=forms, limit=limit))


def _fetch_raw_page(client: Any, command: Command, page: str) -> Any | None:
    try:
        return client.get_cgi_page(page)
    except (RouterAuthError, RouterSessionPoolFullError):
        raise
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
        raise session_pool_full_error(waited_ms=pool_full.waited_ms or 0, retry_count=pool_full.retry_count or 0)

    if command.raw and not write_artifacts and not command.options.json:
        sys.stdout.write((pages[0].raw_html if pages else None) or "")
        return []

    if write_artifacts:
        return write_sweep_artifacts(pages, command.out_dir or "")

    if include_parsed or include_forms or include_raw:
        return pages
    return [strip_large_payloads(page) for page in pages]


def _fetch_snapshot_pages(client: Any) -> tuple[dict[str, ParsedPage], list[ParsedPageResult]]:
    parsed: dict[str, ParsedPage] = {}
    failures: list[ParsedPageResult] = []
    for page in SNAPSHOT_PAGES:
        result = fetch_parsed_page(client, page, include_secrets=True)
        if result.ok and result.parsed is not None:
            parsed[page] = result.parsed
        else:
            failures.append(result)
    return parsed, failures


def _snapshot_meta(command: Command) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return {"ts": ts, "router_host": command.options.host}


def _report_fetch_failure(command: Command, result: ParsedPageResult) -> None:
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


_FLAG = "store_true"
# (flags, kwargs) for every global and per-command option; help strings mirror the TS printHelp text.
_GLOBAL_OPTIONS: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("--host",), {"metavar": "<host>", "help": "Router host. Default: BGW_HOST, ROUTER_IP, or 192.168.1.254"}),
    (("--access-code-stdin",), {"action": _FLAG, "help": "Read the device access code from stdin"}),
    (("--json",), {"action": _FLAG, "help": "Print script-friendly JSON"}),
    (("--include-secrets",), {"action": _FLAG, "help": "Include sensitive local output instead of redacting it"}),
    (
        ("--timeout",),
        {"metavar": "<ms>", "help": "Request timeout. Default: 15000 (home/lanstatistics: 45000 unless set)"},
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
    (("--out",), {"dest": "out_dir", "metavar": "<dir>", "help": "Sweep artifact dir; for dump the output FILE path"}),
    (("--prune",), {"action": _FLAG, "help": "Let restore remove router services/forwards missing from the dump"}),
    (
        ("--all-clients",),
        {
            "action": _FLAG,
            "help": "dump: keep every client row in the documentary IP Allocation table (default: Fixed only)",
        },
    ),
    (("--include-lan",), {"action": _FLAG, "help": "Include the LAN (etherlan) form in diff/restore"}),
)


def _common_options() -> argparse.ArgumentParser:
    """Every global and per-command flag; attached to the root and each subparser so flags go anywhere.

    Defaults are SUPPRESS so a subparser never clobbers a value parsed before the command name.
    """
    common = argparse.ArgumentParser(add_help=False)
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
    ("dump", "dump [--out <file>]: capture a configuration snapshot to an owner-only JSON file."),
    ("diff", "diff <dumpfile> [--include-lan]: compare a dump with the live router."),
    ("restore", "restore <dumpfile> [--prune] [--include-lan] [--commit --confirm RESTORE]."),
    ("help", "Show the full help text."),
    ("fixtures-capture", argparse.SUPPRESS),
)


def build_parser() -> _Parser:
    common = _common_options()
    parser = _Parser(prog="bgwcli", parents=[common], add_help=False)
    parser.add_argument("-h", "--help", action=_FLAG, dest="help_flag", default=argparse.SUPPRESS, help="Show help")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, description in _SUBCOMMANDS:
        sub = subparsers.add_parser(
            name, help=description, description=description, parents=[common], aliases=SECTION_COMMANDS.get(name, ())
        )
        sub.add_argument("args", nargs="*", help="positional arguments for the command")
    return parser


_ALIAS_TO_COMMAND = {alias: name for name, aliases in SECTION_COMMANDS.items() for alias in aliases}


def parse_args(argv: list[str]) -> Command:
    parser = build_parser()
    namespace, leftover = parser.parse_known_args(argv)
    stray_flags = [token for token in leftover if token.startswith("-")]
    if stray_flags:
        raise UsageError(f"unrecognized arguments: {' '.join(stray_flags)}")

    name = getattr(namespace, "command", None)
    if getattr(namespace, "help_flag", False) or name is None:
        return Command(name="help", args=[], options=env_default_options())
    name = _ALIAS_TO_COMMAND.get(name, name)
    args = [*getattr(namespace, "args", []), *leftover]

    def opt(key: str, default: Any = None) -> Any:
        return getattr(namespace, key, default)

    options = env_default_options()
    if opt("host"):
        options.host = opt("host")
    if opt("access_code_stdin", False):
        options.access_code_stdin = True
    if opt("json", False):
        options.json = True
    if opt("include_secrets", False):
        options.include_secrets = True
    if opt("timeout") is not None:
        options.timeout_ms = _numeric(opt("timeout"), "--timeout", 1)
        options.timeout_explicit = True
    if opt("strict_tls", False):
        options.insecure_tls = False
    if opt("wait_for_session", False):
        options.wait_for_session = True
    if opt("session_wait_timeout") is not None:
        options.session_wait_timeout_ms = _numeric(opt("session_wait_timeout"), "--session-wait-timeout", 0)
    if opt("session_wait_interval") is not None:
        options.session_wait_interval_ms = _numeric(opt("session_wait_interval"), "--session-wait-interval", 1)

    pages = None
    if opt("pages") is not None:
        pages = [page.strip() for page in opt("pages").split(",") if page.strip()]

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
        include_lan=opt("include_lan", False),
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
  bgwcli dump [--out <file>] [--all-clients]
    The documentary IP Allocation table keeps only Fixed Allocation rows by default;
    --all-clients keeps every client row.
    Capture services, NAT/Gaming forwards, host reservations, Advanced Wi-Fi
    and firewall/Wi-Fi/LAN forms to an owner-only JSON file. For dump, --out
    is the output FILE path.

  bgwcli diff <dumpfile> [--include-lan]
    Compare a dump with the live router. Exit 0 identical, 1 different, 2 error.

  bgwcli restore <dumpfile> [--prune] [--include-lan]
    Dry-run the plan that replays missing services/forwards/reservations and
    changed forms, in order: services -> forwards -> reservations -> firewall
    advanced -> Advanced Wi-Fi -> LAN.

  bgwcli restore <dumpfile> --commit --confirm RESTORE
    Apply the plan, then re-diff. Exit 0 once everything in the dump is present
    on the router (router-only extras are left alone unless --prune is given).
    Note: reservations are never released by --prune, only added or corrected.
    LAN settings stay untouched without --include-lan. Removes are deferred on
    any page that still has an add to make, so adds and prunes may need two runs.
    A step shown as applied means the router accepted the POST; the diff printed
    at the end is the authoritative success signal.

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
  --include-lan             Include the LAN (etherlan) form in diff/restore
  --timeout <ms>            Request timeout. Default: 15000
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
  An active local pool cooldown always fails fast, including with --wait-for-session.
  Agent bursts reuse a short-lived local router session cache under a per-host
  lock to avoid filling the web session pool. Env:
  BGW_SESSION_CACHE_TTL_MS, BGW_SESSION_POOL_COOLDOWN_MS, BGW_SESSION_LOCK_TIMEOUT_MS.
  JSON dry-runs include operation, dryRun, committed, page, guarded, dangerous,
  confirmation, commitCommand, payload, and changes/result fields where applicable.

Sections: {", ".join(list_sections())}
"""
