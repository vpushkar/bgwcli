"""Diagnostics ported from src/diagnostics.ts (ping/traceroute/nslookup submit plans, DIAG token)."""

from __future__ import annotations

import pytest

from bgwcli.diagnostics import (
    DIAG_CONFIRM_TOKEN,
    DIAGNOSTIC_BUTTONS,
    DIAGNOSTIC_KINDS,
    build_diagnostic_plan,
    diagnostic_button,
    extract_diagnostic_result,
    is_diagnostic_kind,
)
from bgwcli.types import ParsedPage, ParsedTextarea


def test_kinds_buttons_and_token_match_typescript():
    assert DIAGNOSTIC_KINDS == ("ping", "traceroute", "nslookup")
    assert DIAGNOSTIC_BUTTONS == {"ping": "Ping", "traceroute": "Trace", "nslookup": "Lookup"}
    assert DIAG_CONFIRM_TOKEN == "DIAG"
    assert diagnostic_button("ping") == "Ping"
    assert diagnostic_button("traceroute") == "Trace"
    assert diagnostic_button("nslookup") == "Lookup"


def test_is_diagnostic_kind():
    assert is_diagnostic_kind("ping")
    assert is_diagnostic_kind("traceroute")
    assert is_diagnostic_kind("nslookup")
    assert not is_diagnostic_kind("Ping")
    assert not is_diagnostic_kind("speed-test")
    assert not is_diagnostic_kind(None)
    assert not is_diagnostic_kind("")


def _diag_page() -> ParsedPage:
    return ParsedPage(page="diag", title="Troubleshoot", heading="Troubleshoot")


def test_build_diagnostic_plan_delegates_to_submit_plan_with_target_assignment():
    calls = []

    def fake_build_submit_plan(page, parsed, button_name, assignments, include_secrets=False):
        calls.append((page, parsed, button_name, list(assignments), include_secrets))
        return "PLAN"

    parsed = _diag_page()
    plan = build_diagnostic_plan(parsed, "ping", "example.com", build_submit_plan=fake_build_submit_plan)
    assert plan == "PLAN"
    assert calls == [("diag", parsed, "Ping", ["WebAddress=example.com"], False)]


def test_build_diagnostic_plan_adds_protocol_preference_and_include_secrets():
    calls = []

    def fake_build_submit_plan(page, parsed, button_name, assignments, include_secrets=False):
        calls.append((page, button_name, list(assignments), include_secrets))
        return None

    parsed = _diag_page()
    build_diagnostic_plan(parsed, "traceroute", "1.1.1.1", protocol="IPv6", build_submit_plan=fake_build_submit_plan)
    build_diagnostic_plan(
        parsed, "nslookup", "att.net", protocol="IPv4", include_secrets=True, build_submit_plan=fake_build_submit_plan
    )
    assert calls == [
        ("diag", "Trace", ["WebAddress=1.1.1.1", "protopref=IPv6"], False),
        ("diag", "Lookup", ["WebAddress=att.net", "protopref=IPv4"], True),
    ]


def test_build_diagnostic_plan_rejects_unknown_kind():
    with pytest.raises(KeyError):
        build_diagnostic_plan(_diag_page(), "speed", "x", build_submit_plan=lambda *a, **k: None)


def test_extract_diagnostic_result_prefers_progress_textarea_then_value_fallback():
    page = ParsedPage(
        page="diag",
        title="t",
        heading="h",
        values={"Field ProgressWindow": "from values"},
        textareas=[ParsedTextarea(name="ProgressWindow", value="PING example.com: 3 packets", sensitive=False)],
    )
    assert extract_diagnostic_result(page) == "PING example.com: 3 packets"

    empty_textarea = ParsedPage(
        page="diag",
        title="t",
        heading="h",
        values={"Field ProgressWindow": "from values"},
        textareas=[ParsedTextarea(name="ProgressWindow", value="", sensitive=False)],
    )
    assert extract_diagnostic_result(empty_textarea) == "from values"

    other_textarea = ParsedPage(
        page="diag",
        title="t",
        heading="h",
        textareas=[ParsedTextarea(name="Other", value="ignored", sensitive=False)],
    )
    assert extract_diagnostic_result(other_textarea) == ""


class _SequenceClient:
    """get_cgi_page returns the next canned diag page body on every call."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def get_cgi_page(self, page, *, auth=True):
        from bgwcli.types import HttpResponse

        self.calls += 1
        body = self.bodies.pop(0) if self.bodies else ""
        return HttpResponse(200, "OK", {}, body, f"https://r/cgi-bin/{page}.ha")


def _diag_html(progress: str) -> str:
    return (
        '<html><body><form method="post" action="/cgi-bin/diag.ha"><input type="hidden" name="nonce" value="n">'
        f'<textarea name="ProgressWindow">{progress}</textarea>'
        '<input type="submit" name="Ping" value="Ping"></form></body></html>'
    )


def test_poll_diagnostic_result_waits_until_the_progress_window_is_non_empty_and_stable():
    from bgwcli.diagnostics import poll_diagnostic_result

    bodies = ["", "PING 8.8.8.8", "PING 8.8.8.8\n64 bytes", "PING 8.8.8.8\n64 bytes"]
    client = _SequenceClient([_diag_html(b) for b in bodies])
    slept: list[float] = []
    text, page = poll_diagnostic_result(client, timeout_ms=15000, interval_ms=1000, sleep=slept.append)
    assert text == "PING 8.8.8.8 64 bytes"  # parser normalizes textarea whitespace
    assert page is not None and page.page == "diag"
    assert client.calls == 4  # empty, partial, full, full (stable) -> stop
    assert slept == [1.0, 1.0, 1.0, 1.0]  # one interval before every poll, none after the last


def test_poll_diagnostic_result_gives_up_at_the_timeout_with_empty_text():
    from bgwcli.diagnostics import poll_diagnostic_result

    client = _SequenceClient([_diag_html("")] * 50)
    slept: list[float] = []
    text, page = poll_diagnostic_result(client, timeout_ms=3000, interval_ms=1000, sleep=slept.append)
    assert text == ""
    assert client.calls == 3 and sum(slept) == 3.0


def test_poll_diagnostic_result_returns_the_last_output_when_the_router_clears_the_window():
    """Live 2026-09-20: ping output appears ~3 s after the POST, grows for a few seconds and the
    gateway then blanks ProgressWindow. Growing-then-cleared must yield the last text seen."""
    from bgwcli.diagnostics import poll_diagnostic_result

    bodies = ["", "PING 8.8.8.8", "PING 8.8.8.8 seq=0", "PING 8.8.8.8 seq=0 seq=1", "", ""]
    client = _SequenceClient([_diag_html(b) for b in bodies])
    text, page = poll_diagnostic_result(client, timeout_ms=15000, interval_ms=500, sleep=lambda s: None)
    assert text == "PING 8.8.8.8 seq=0 seq=1"
    assert client.calls == 5  # stops at the first empty poll after content


def test_poll_diagnostic_result_returns_the_last_output_at_the_deadline_while_still_growing():
    from bgwcli.diagnostics import poll_diagnostic_result

    bodies = [_diag_html(f"hop {i}") for i in range(1, 60)]
    client = _SequenceClient(bodies)
    text, _ = poll_diagnostic_result(client, timeout_ms=2000, interval_ms=500, sleep=lambda s: None)
    assert text == "hop 4" and client.calls == 4


def test_poll_diagnostic_result_defaults_to_half_second_polls():
    import inspect

    from bgwcli.diagnostics import poll_diagnostic_result

    assert inspect.signature(poll_diagnostic_result).parameters["interval_ms"].default == 500
