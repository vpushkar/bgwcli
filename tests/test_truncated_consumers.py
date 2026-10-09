"""A page the parser cut at a bound is never read as if it were whole: every cut sets `truncated`, and
the readers that build a list, a log, a save verdict or a sweep receipt from a page refuse a cut one."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from bgwcli import sweep
from bgwcli.audit import expected_fixture
from bgwcli.devices import fetch_device_list
from bgwcli.parser import (
    MAX_ELEMENTS,
    MAX_NESTING,
    MAX_TEXT_PARTS,
    TruncatedPageError,
    parse_document,
    parse_logs,
    parse_page,
)
from bgwcli.save_confirmation import no_changes_notice, save_notification
from bgwcli.types import to_json_dict

NESTED = "<table><tr><td>" * (MAX_NESTING + 5) + "x"
HEADINGS = "<h2>a" * (MAX_NESTING + 5)
LONG_TEXT = "<table><tr><td>Note</td><td>" + "<i>w</i>" * (MAX_TEXT_PARTS * 2) + "</td></tr></table>"
CHARS = "<table><tr><td>Note</td><td>" + "w" * 100_001 + "</td></tr></table>"
SPAN = '<table><tr><td colspan="100">Mode</td><td>x</td></tr></table>'
GRID = "<table>" + '<tr><td colspan="64">a</td></tr>' * 4100 + "</table>"
ELEMENTS = "<b>x</b>" * (MAX_ELEMENTS + 1)


@pytest.mark.parametrize(
    "html",
    [NESTED, HEADINGS, LONG_TEXT, CHARS, SPAN, GRID, ELEMENTS],
    ids=["table nesting", "heading nesting", "text pieces", "text characters", "span clamp", "grid", "elements"],
)
def test_every_parser_cut_marks_the_page_truncated(html):
    page = parse_page("x", html)
    assert page.truncated is True
    assert to_json_dict(page)["truncated"] is True


def test_a_page_within_every_bound_is_not_marked():
    html = "<table><tr><td>Mode</td><td>Auto</td></tr></table>" + "<h2>a</h2>" * 50
    assert parse_page("x", html).truncated is None


def test_table_nesting_past_the_bound_marks_the_document():
    assert parse_document(NESTED).truncated is True
    assert parse_document("<table><tr><td>" * (MAX_NESTING - 1) + "x").truncated is False


# --- devices -----------------------------------------------------------------------------------


@dataclass
class _Response:
    body: str
    status_code: int = 200


@dataclass
class _Client:
    pages: dict[str, str]
    calls: list[str] = field(default_factory=list)

    def get_cgi_page(self, page: str, **kwargs):
        self.calls.append(page)
        return _Response(self.pages[page])


DEVICE_ROW = "<tr><td>on</td><td>192.168.1.5 / laptop</td><td>x</td><td>aa:bb:cc:dd:ee:ff</td><td>wifi</td></tr>"
DEVICE_TABLE = (
    "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>IPv6</th><th>MAC Address</th><th>Connection</th></tr>"
    + DEVICE_ROW
    + "</table>"
)
IPALLOC_TABLE = (
    "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>MAC Address</th><th>Allocation</th></tr>"
    "<tr><td>on</td><td>192.168.1.5 / laptop</td><td>aa:bb:cc:dd:ee:ff</td><td>dhcp</td></tr></table>"
)


def test_a_device_page_cut_by_the_parser_falls_back_to_ipalloc():
    cut = DEVICE_TABLE + "<b>x</b>" * MAX_ELEMENTS
    client = _Client({"devices": cut, "ipalloc": IPALLOC_TABLE})
    result = fetch_device_list(client)
    assert result.fallback is True
    assert "cut by the parser" in result.error
    assert [device.name for device in result.devices] == ["laptop"]
    assert client.calls == ["devices", "ipalloc"]


def test_an_ipalloc_fallback_cut_by_the_parser_reads_nothing():
    cut = IPALLOC_TABLE + "<b>x</b>" * MAX_ELEMENTS
    client = _Client({"devices": "<p>busy</p>", "ipalloc": cut})
    result = fetch_device_list(client)
    assert result.unread is True
    assert "cut by the parser" in result.fallback_error
    assert result.devices == []


def test_an_uncut_device_page_is_read_as_before():
    result = fetch_device_list(_Client({"devices": DEVICE_TABLE}))
    assert result.fallback is False
    assert [device.name for device in result.devices] == ["laptop"]


# --- logs --------------------------------------------------------------------------------------

LOG_HEAD = "<tr><th>ID</th><th>Time</th><th>Source</th><th>Destination</th><th>Protocol</th><th>Reason</th></tr>"
LOG_ROW = "<tr><td>1</td><td>t</td><td>s</td><td>d</td><td>tcp</td><td>r</td></tr>"


def test_a_log_page_cut_by_the_parser_is_refused():
    with pytest.raises(TruncatedPageError, match="log page was cut"):
        parse_logs(f"<table>{LOG_HEAD}{LOG_ROW}</table>" + "<b>x</b>" * MAX_ELEMENTS)


def test_a_whole_log_page_is_read_as_before():
    entries = parse_logs(f"<table>{LOG_HEAD}{LOG_ROW}</table>")
    assert [entry.id for entry in entries] == ["1"]
    assert parse_logs("<p>busy</p>") is None


# --- save notices ------------------------------------------------------------------------------

SAVED = '<div id="error-message-text">Changes saved</div>'


def test_a_save_answer_cut_before_its_notice_cannot_be_read():
    cut = "<b>x</b>" * MAX_ELEMENTS + SAVED
    with pytest.raises(TruncatedPageError):
        save_notification("wconfig", cut)
    with pytest.raises(TruncatedPageError):
        no_changes_notice(cut)


def test_a_notice_read_before_the_cut_is_still_reported():
    assert save_notification("wconfig", SAVED + "<b>x</b>" * MAX_ELEMENTS).saved is True


def test_a_whole_answer_without_a_notice_is_not_an_error():
    assert save_notification("wconfig", "<p>hi</p>").saved is False
    assert no_changes_notice("<p>hi</p>") is None


# --- sweep and fixture receipts ------------------------------------------------------------------


@dataclass
class _SweepClient:
    body: str
    calls: list[str] = field(default_factory=list)

    def get_cgi_page(self, page: str, **kwargs):
        self.calls.append(page)
        return _Response(self.body)


def test_a_sweep_page_the_parser_cut_is_a_failed_page_with_a_reason_and_a_receipt():
    client = _SweepClient(LONG_TEXT)
    pages = sweep.sweep_router(client, sweep.SweepOptions(pages=["diag"], use_fallbacks=False, include_parsed=True))
    assert [page.ok for page in pages] == [False]
    assert pages[0].truncated is True
    assert "cut by the parser" in pages[0].error
    receipt = to_json_dict(pages[0])
    assert receipt["truncated"] is True and receipt["parsed"]["truncated"] is True
    assert sweep.sweep_exit_code(pages) == 2


def test_an_uncut_sweep_page_has_no_truncated_receipt():
    pages = sweep.sweep_router(
        _SweepClient("<table><tr><td>Mode</td><td>Auto</td></tr></table>"),
        sweep.SweepOptions(pages=["diag"], use_fallbacks=False),
    )
    assert pages[0].ok is True and pages[0].truncated is None
    assert "truncated" not in to_json_dict(pages[0])


def test_the_fixture_receipt_of_a_cut_page_says_so():
    parsed = parse_page("x", LONG_TEXT)
    receipt = to_json_dict(expected_fixture("x", parsed, page_loads=False))
    assert receipt["truncated"] is True
    ok = to_json_dict(expected_fixture("x", parse_page("x", "<p>a</p>"), page_loads=True))
    assert "truncated" not in ok
    json.dumps(receipt)
