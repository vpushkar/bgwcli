"""Raw ownership provenance stays private and is consumed only during initial preflight."""

import json

import pytest
from test_allocation_preflight import (
    HOLDER,
    IP,
    REQUESTS,
    Router,
    devices_html,
    ipalloc_html,
    response,
)
from test_allocation_preflight import (
    clock as _clock,
)

from bgwcli import allocation_preflight as preflight
from bgwcli.cli import _fetch_snapshot_pages
from bgwcli.errors import UsageError
from bgwcli.types import to_json_dict

clock = _clock


def test_initial_preflight_reuses_fresh_raw_snapshot_without_exposing_html(clock):
    body = ipalloc_html([(IP, HOLDER, "holder", "off")]) + "<!--PRIVATE-RAW-SENTINEL-->"
    router = Router(response(devices_html()), initial_allocation=response(body))
    parsed, failures = _fetch_snapshot_pages(router, ["ipalloc"])
    assert not failures
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS, snapshot_pages=parsed)
    assert pending.conflicts[0].holder_mac == HOLDER
    assert router.gets == ["ipalloc", "devices"]
    assert "PRIVATE-RAW-SENTINEL" not in json.dumps(to_json_dict(parsed))
    assert "PRIVATE-RAW-SENTINEL" not in json.dumps(preflight.allocation_preflight_report(pending))
    # The capture is consumed; another inspection cannot reuse evidence from a past invocation.
    preflight.inspect_allocation_conflicts(router, REQUESTS, snapshot_pages=parsed)
    assert router.gets == ["ipalloc", "devices", "devices", "ipalloc"]


@pytest.mark.parametrize("case", ["aged", "another-client", "plain-parsed", "slow-devices"])
def test_initial_preflight_refetches_without_fresh_same_client_raw_provenance(clock, case):
    router = Router(response(devices_html()), initial_allocation=response(ipalloc_html()))
    parsed, _ = _fetch_snapshot_pages(router, ["ipalloc"])
    if case == "aged":
        clock.now = 60
    elif case == "another-client":
        router = Router(response(devices_html()), initial_allocation=response(ipalloc_html()))
    elif case == "plain-parsed":
        parsed = dict(parsed)
    elif case == "slow-devices":
        original = router.get_cgi_page

        def slow_get(page):
            result = original(page)
            if page == "devices":
                clock.now = 60
            return result

        router.get_cgi_page = slow_get
    assert preflight.inspect_allocation_conflicts(router, REQUESTS, snapshot_pages=parsed).conflicts == []
    assert router.gets[-2:] == ["devices", "ipalloc"]


def test_reused_raw_response_still_rejects_row_the_parsed_snapshot_would_lose(clock):
    malformed = ipalloc_html().replace("</table>", "<tr><td>Processing</td></tr></table>")
    router = Router(response(devices_html()), initial_allocation=response(malformed))
    parsed, failures = _fetch_snapshot_pages(router, ["ipalloc"])
    assert not failures
    with pytest.raises(UsageError, match="incomplete"):
        preflight.inspect_allocation_conflicts(router, REQUESTS, snapshot_pages=parsed)
    assert router.gets == ["ipalloc", "devices"]
    assert not router.posts


def test_clear_always_refreshes_both_sources_after_cached_initial_read(clock):
    held = [(IP, HOLDER, "old", "off")]
    router = Router(
        response(devices_html()),
        response(ipalloc_html()),
        initial_allocation=response(ipalloc_html(held)),
        clock=clock,
    )
    parsed, _ = _fetch_snapshot_pages(router, ["ipalloc"])
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS, snapshot_pages=parsed)
    preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert router.gets == ["ipalloc", "devices", "devices", "ipalloc"]
    assert router.read_starts == [("devices", 60), ("ipalloc", 60)]
    assert len(router.posts) == 1
