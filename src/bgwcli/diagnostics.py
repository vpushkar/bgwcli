"""Diagnostics form submissions (ping / traceroute / nslookup) on the diag page. Port of src/diagnostics.ts.

All three kinds share the single confirmation token DIAG.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from .types import ParsedPage

DiagnosticKind = Literal["ping", "traceroute", "nslookup"]
DiagnosticProtocol = Literal["IPv4", "IPv6"]

DIAGNOSTIC_KINDS: tuple[DiagnosticKind, ...] = ("ping", "traceroute", "nslookup")
DIAGNOSTIC_BUTTONS: dict[str, str] = {"ping": "Ping", "traceroute": "Trace", "nslookup": "Lookup"}
DIAG_CONFIRM_TOKEN = "DIAG"
DIAG_PAGE = "diag"

# (page, parsed, button_name, assignments, include_secrets) -> MutationPlan
SubmitPlanBuilder = Callable[[str, ParsedPage, str, list[str], bool], Any]


def is_diagnostic_kind(value: object) -> bool:
    return isinstance(value, str) and value in DIAGNOSTIC_KINDS


def diagnostic_button(kind: str) -> str:
    return DIAGNOSTIC_BUTTONS[kind]


def build_diagnostic_plan(
    parsed: ParsedPage,
    kind: str,
    target: str,
    protocol: DiagnosticProtocol | None = None,
    include_secrets: bool = False,
    *,
    build_submit_plan: SubmitPlanBuilder | None = None,
):
    """Build the submit plan for one diagnostic run.

    Returns a mutations.MutationPlan; `build_submit_plan` is injectable for tests and defaults to
    bgwcli.mutations.build_submit_plan (imported lazily).
    """
    button = diagnostic_button(kind)
    assignments = [f"WebAddress={target}"]
    if protocol:
        assignments.append(f"protopref={protocol}")
    if build_submit_plan is None:
        from .mutations import build_submit_plan as _build_submit_plan

        build_submit_plan = _build_submit_plan
    return build_submit_plan(DIAG_PAGE, parsed, button, assignments, include_secrets)


def extract_diagnostic_result(parsed: ParsedPage) -> str:
    """The router writes diagnostic output into the ProgressWindow textarea; fall back to the parsed value."""
    for textarea in parsed.textareas:
        if textarea.name == "ProgressWindow" and textarea.value:
            return textarea.value
    return parsed.values.get("Field ProgressWindow", "")


_sleep = time.sleep  # monkeypatch seam for tests


def poll_diagnostic_result(
    client,
    *,
    timeout_ms: int = 15000,
    interval_ms: int = 500,
    include_secrets: bool = False,
    sleep=None,
):
    """Follow up a diagnostic POST: the gateway answers 302 with an empty body, fills the
    ProgressWindow textarea asynchronously (~3 s later for ping), lets it grow while the tool
    runs, and then BLANKS it once the run is over (observed live 2026-09-20). So: remember the
    last non-empty text and return it when the window clears, when it stops changing between two
    polls, or when the deadline passes. Returns (text, last_page_with_content); "" if none seen.
    """
    from .parser import parse_page

    do_sleep = sleep if sleep is not None else _sleep
    waited = 0
    last_text = ""
    last_page = None
    while waited < timeout_ms:
        do_sleep(interval_ms / 1000)
        waited += interval_ms
        response = client.get_cgi_page(DIAG_PAGE)
        page = parse_page(DIAG_PAGE, response.body, include_secrets=include_secrets)
        text = extract_diagnostic_result(page)
        if text:
            if text == last_text:
                return text, page  # stable: the run is finished and still displayed
            last_text, last_page = text, page
        elif last_text:
            return last_text, last_page  # cleared after content: the run finished
    return last_text, last_page
