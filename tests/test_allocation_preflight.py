"""IP ownership preflight uses only synthetic HTML and a controlled clock."""

from __future__ import annotations

import pytest

from bgwcli import allocation_preflight as preflight
from bgwcli.errors import UsageError
from bgwcli.snapshot import SnapshotReservation
from bgwcli.types import HttpResponse

TARGET = "02:0a:0b:0c:0d:02"
HOLDER = "02:0a:0b:0c:0d:03"
IP = "192.168.1.64"
REQUESTS = [SnapshotReservation(TARGET, IP)]
ERROR = '<img id="error-message-icon" src="/images/icon_error.png"><div id="error-message-text">Cannot rescan now</div>'


def devices_html(rows=(), *, clear=True, disabled=False):
    button = (
        f'<input type="submit" name="Clear" value="Clear &amp; Rescan"{" disabled" if disabled else ""}>'
        if clear
        else ""
    )
    body = "".join(
        f"<tr><td>{status}</td><td>{ip} / {name}</td><td></td><td>{mac}</td><td>Ethernet</td></tr>"
        for ip, mac, name, status in rows
    )
    return (
        '<html><head><title>Device List</title></head><body><form method="post" action="/cgi-bin/devices.ha">'
        f'<input type="hidden" name="nonce" value="ab12">{button}'
        "<table><tr><th>Status</th><th>IPv4 Address / Name</th><th>IPv6</th>"
        f"<th>MAC Address</th><th>Connection Type</th></tr>{body}</table></form></body></html>"
    )


def key_value_devices_html(rows):
    body = "".join(f"<tr><td>{key}</td><td>{value}</td></tr>" for key, value in rows)
    return (
        '<html><head><title>Device List</title></head><body><form action="/cgi-bin/devices.ha">'
        '<input type="submit" name="Clear" value="Clear Device List">'
        f"<table>{body}</table></form></body></html>"
    )


def ipalloc_html(rows=()):
    body = "".join(
        f"<tr><td>{ip} / {name}</td><td>{mac}</td><td>{status}</td><td>DHCP Allocation</td></tr>"
        for ip, mac, name, status in rows
    )
    return (
        "<html><head><title>IP Allocation</title></head><body><table>"
        "<tr><th>IPv4 Address / Name</th><th>MAC Address</th><th>Status</th><th>Allocation</th></tr>"
        f"{body}</table></body></html>"
    )


def response(body, status=200):
    return HttpResponse(status, "OK", {}, body, "https://synthetic.invalid/")


class Router:
    def __init__(
        self, devices, *allocation_states, initial_allocation=None, device_states=(), post_response=None, clock=None
    ):
        self.devices = devices
        self.initial_allocation = initial_allocation or response(ipalloc_html())
        self.device_states = device_states or (devices,)
        self.allocation_states = allocation_states or (self.initial_allocation,)
        self.post_response = post_response or response("")
        self.clock = clock
        self.gets = []
        self.posts = []
        self.read_times = []
        self.read_starts = []
        self.counts = {"devices": 0, "ipalloc": 0}

    def get_cgi_page(self, page):
        self.gets.append(page)
        assert page in ("devices", "ipalloc")
        if not self.posts:
            item = self.devices if page == "devices" else self.initial_allocation
        else:
            if self.clock:
                self.read_starts.append((page, self.clock.now))
                if page == "ipalloc":
                    self.read_times.append(self.clock.now)
            count = self.counts[page]
            assert count < 40, "rescan polling exceeded the bounded time window"
            states = self.device_states if page == "devices" else self.allocation_states
            item = states[min(count, len(states) - 1)]
            self.counts[page] += 1
        if isinstance(item, Exception):
            raise item
        return item

    def post_cgi_page(self, page, fields):
        self.posts.append((page, dict(fields)))
        return self.post_response


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def __init__(self):
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            assert seconds > 0
            self.sleeps.append(seconds)
            self.now += seconds

    fake = Clock()
    monkeypatch.setattr(preflight, "monotonic", fake.monotonic, raising=False)
    monkeypatch.setattr(preflight, "sleep", fake.sleep, raising=False)
    return fake


def test_inspection_includes_offline_holders_and_the_rendered_clear_payload():
    router = Router(response(devices_html([(IP, HOLDER.upper(), "old-host", "off")])))
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    assert result.conflicts == [preflight.AllocationConflict(IP, TARGET, HOLDER, "old-host", "off")]
    assert result.clear_payload == {"Clear": "Clear & Rescan"}
    assert router.gets == ["devices", "ipalloc"]
    assert router.posts == []


@pytest.mark.parametrize("mac", [HOLDER.upper(), TARGET.upper()], ids=["different-owner", "same-owner"])
def test_supported_key_value_device_layout_preserves_exact_mac_ownership(mac):
    html = key_value_devices_html(
        [("MAC Address", mac), ("IPv4 Address / Name", f"{IP} / renamed-device"), ("Status", "off")]
    )
    router = Router(response(html))
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    expected = (
        [] if mac.lower() == TARGET else [preflight.AllocationConflict(IP, TARGET, HOLDER, "renamed-device", "off")]
    )
    assert result.conflicts == expected
    assert result.clear_payload == {"Clear": "Clear Device List"}
    assert router.gets == ["devices", "ipalloc"]
    assert router.posts == []


@pytest.mark.parametrize(
    "html",
    [
        devices_html().replace("</table>", f"<tr><td>off</td><td>{IP} / missing-mac</td><td></td></tr></table>"),
        key_value_devices_html(
            [
                ("IPv4 Address / Name", f"{IP} / orphan-device"),
                ("Status", "off"),
                ("MAC Address", HOLDER),
                ("IPv4 Address / Name", "192.168.1.90 / valid-device"),
            ]
        ),
        key_value_devices_html([("MAC Address", ""), ("IPv4 Address / Name", f"{IP} / missing-mac")]),
        key_value_devices_html(
            [
                ("MAC Address", TARGET),
                ("IPv4 Address / Name", "192.168.1.90 / first-device"),
                ("IPv4 Address / Name", f"{IP} / second-device-without-mac"),
            ]
        ),
        key_value_devices_html(
            [("MAC Address", TARGET), ("IPv4 Address / Name", "192.168.1.90 / valid-device")]
        ).replace(
            "</form>",
            f"<table><tr><td>IPv4 Address / Name</td><td>{IP} / missing-mac-device</td></tr>"
            "<tr><td>Status</td><td>off</td></tr></table></form>",
        ),
    ],
    ids=[
        "truncated-wide-row",
        "orphan-key-value-record",
        "empty-key-value-mac",
        "missing-next-key-value-mac",
        "orphan-separate-key-value-table",
    ],
)
def test_requested_ip_with_incomplete_device_ownership_is_never_silently_skipped(html):
    router = Router(response(html))
    with pytest.raises(UsageError, match="[Vv]alidat|ownership|MAC"):
        preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert router.gets == ["devices"]
    assert router.posts == []


@pytest.mark.parametrize("phase", ["initial", "rescan"])
def test_requested_key_value_address_cannot_be_overwritten_by_later_unrelated_address(clock, phase):
    ambiguous = response(
        key_value_devices_html(
            [
                ("MAC Address", HOLDER),
                ("IPv4 Address / Name", f"{IP} / held-address"),
                ("IPv4 Address / Name", "192.168.1.90 / unrelated-address"),
            ]
        )
    )
    if phase == "initial":
        router = Router(ambiguous)
        with pytest.raises(UsageError, match=IP):
            preflight.inspect_allocation_conflicts(router, REQUESTS)
        assert router.posts == []
    else:
        router = Router(
            response(devices_html([(IP, HOLDER, "old", "off")])),
            response(ipalloc_html()),
            device_states=(ambiguous,),
            clock=clock,
        )
        pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
        with pytest.raises(UsageError, match=IP):
            preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
        assert len(router.posts) == 1


def test_same_mac_is_not_a_conflict_and_unrelated_invalid_mac_is_ignored():
    router = Router(
        response(devices_html([(IP, TARGET.upper(), "target", "on"), ("192.168.1.99", "Unknown", "other", "off")]))
    )
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    assert result.conflicts == []
    assert router.posts == []


def test_duplicate_hostnames_do_not_hide_a_different_mac_holder():
    router = Router(
        response(devices_html([(IP, HOLDER, "same-host", "off"), ("192.168.1.90", TARGET, "same-host", "on")]))
    )
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert result.conflicts == [preflight.AllocationConflict(IP, TARGET, HOLDER, "same-host", "off")]


def test_renamed_device_with_same_mac_is_not_a_conflict():
    router = Router(response(devices_html([(IP, TARGET.upper(), "renamed-host", "off")])))
    assert preflight.inspect_allocation_conflicts(router, REQUESTS).conflicts == []


@pytest.mark.parametrize("target_mac", [TARGET.upper(), f" {TARGET.upper()} "])
def test_requested_mac_normalization_keeps_renamed_same_mac_device_out_of_conflicts(target_mac):
    router = Router(response(devices_html([(IP, f" {TARGET} ", "renamed-host", "off")])))
    requests = [SnapshotReservation(target_mac, IP)]
    assert preflight.inspect_allocation_conflicts(router, requests).conflicts == []


def test_clear_control_uses_its_live_value_even_without_rescan_in_the_label():
    html = devices_html([(IP, HOLDER, "old", "off")]).replace("Clear &amp; Rescan", "Clear Device List")
    router = Router(response(html))
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert result.clear_payload == {"Clear": "Clear Device List"}


def test_empty_reservation_selection_performs_no_get():
    router = Router(response("<html/>"))
    result = preflight.inspect_allocation_conflicts(router, [])

    assert result.conflicts == []
    assert router.gets == router.posts == []


@pytest.mark.parametrize("disabled", [False, True], ids=["missing", "disabled"])
def test_missing_or_disabled_clear_is_reported_before_any_post(disabled):
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")], clear=disabled, disabled=disabled)))
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    assert result.clear_payload is None
    with pytest.raises(UsageError, match="[Cc]lear"):
        preflight.rescan_allocation_conflicts(router, result, log=lambda _: None)
    assert router.posts == []


@pytest.mark.parametrize(
    "bad", [response("<title>Login</title>"), response("<html>Processing</html>"), response(devices_html(), 503)]
)
def test_unreadable_devices_page_never_falls_back_or_counts_as_no_conflict(bad):
    router = Router(bad)
    with pytest.raises(UsageError, match="[Vv]alidat|[Rr]ead|login|HTTP"):
        preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert router.gets == ["devices"]
    assert router.posts == []


def test_invalid_holder_mac_at_requested_ip_is_not_treated_as_free():
    router = Router(response(devices_html([(IP, "Unknown", "old", "off")])))
    with pytest.raises(UsageError, match="MAC|ownership"):
        preflight.inspect_allocation_conflicts(router, REQUESTS)


def test_no_conflicts_is_a_noop_without_clear_or_sleep(clock):
    router = Router(response(devices_html()))
    preflight.rescan_allocation_conflicts(router, preflight.AllocationPreflight([], None), log=lambda _: None)
    assert router.gets == router.posts == []
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "fresh_rows", [[], [(IP, TARGET.upper(), "target", "off")]], ids=["target-absent", "target-owns-ip"]
)
def test_rescan_waits_a_minute_and_allows_absent_target_or_target_ownership(clock, fresh_rows):
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html(fresh_rows)),
        device_states=(response(devices_html(fresh_rows)),),
        clock=clock,
    )
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)

    assert router.posts == [("devices", {"Clear": "Clear & Rescan"})]
    assert router.gets == ["devices", "ipalloc", "devices", "ipalloc"]
    assert router.read_times[0] >= 60
    assert clock.sleeps == [60]


def test_rescan_polls_until_a_different_holder_disappears_without_second_clear(clock):
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html()),
        device_states=(response(devices_html([(IP, HOLDER, "old", "off")])), response(devices_html())),
        clock=clock,
    )
    preflight.rescan_allocation_conflicts(
        router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
    )

    assert router.posts == [("devices", {"Clear": "Clear & Rescan"})]
    assert router.read_times == [60, 65]
    assert clock.sleeps == [60, 5]


def test_persistent_conflict_errors_with_ip_and_holder_after_bounded_polling(clock):
    held = [(IP, HOLDER, "old", "off")]
    router = Router(response(devices_html(held)), response(ipalloc_html(held)), clock=clock)
    with pytest.raises(UsageError) as error:
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )

    assert IP in str(error.value) and HOLDER in str(error.value)
    assert "still" in str(error.value).lower()
    assert router.posts == [("devices", {"Clear": "Clear & Rescan"})]
    assert clock.now == 180
    assert min(router.read_times) >= 60
    assert max(router.read_times) <= 180


def test_no_new_read_starts_at_deadline_and_timeout_reports_the_last_confirmed_holder(clock):
    latest_holder = "02:0a:0b:0c:0d:04"
    router = Router(
        response(devices_html([(IP, HOLDER, "original-holder", "off")])),
        response(ipalloc_html([(IP, latest_holder, "latest-holder", "on")])),
        clock=clock,
    )
    with pytest.raises(UsageError) as error:
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )

    assert router.read_times
    assert all(started < 180 for started in router.read_times)
    assert clock.now == 180
    assert IP in str(error.value) and latest_holder in str(error.value)
    assert "latest-holder" in str(error.value) and "on" in str(error.value)
    assert TARGET in str(error.value)
    assert "still" in str(error.value).lower()
    assert len(router.posts) == 1


@pytest.mark.parametrize(
    "bad",
    [
        response("<title>Login</title>"),
        response("<html>Processing</html>"),
        response(ipalloc_html(), 503),
        response(ipalloc_html([(IP, "Unknown", "old", "off")])),
        response(ipalloc_html().replace("</table>", "<tr><td>Processing</td></tr></table>")),
        RuntimeError("read timed out"),
    ],
    ids=["login", "invalid-html", "http-error", "invalid-mac", "incomplete-row", "transport-error"],
)
def test_unreadable_refreshed_ownership_is_an_explicit_error_not_success(clock, bad):
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")])), bad, clock=clock)
    with pytest.raises(UsageError, match="[Vv]alidat|[Rr]ead|ownership"):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert router.posts == [("devices", {"Clear": "Clear & Rescan"})]


@pytest.mark.parametrize("post_reply", [response(ERROR), response("", 500)])
def test_clear_rejection_fails_without_sleep_or_allocation_refresh(clock, post_reply):
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")])), post_response=post_reply, clock=clock)
    with pytest.raises(UsageError):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert router.gets == ["devices", "ipalloc"]
    assert len(router.posts) == 1
    assert clock.sleeps == []


def test_initial_inspection_catches_fixed_allocation_without_a_matching_device_lease():
    router = Router(
        response(devices_html([("192.168.1.140", HOLDER, "phone", "on")])),
        initial_allocation=response(
            ipalloc_html([(IP, HOLDER, "phone", "off")]).replace("DHCP Allocation", "Fixed Allocation")
        ),
    )
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    assert result.conflicts == [preflight.AllocationConflict(IP, TARGET, HOLDER, "phone", "off", True)]
    assert router.posts == []


def test_initial_inspection_unions_sources_and_deduplicates_normalized_mac_ownership():
    other_holder = "02:0a:0b:0c:0d:04"
    router = Router(
        response(devices_html([(IP, HOLDER.upper(), "device-name", "on")])),
        initial_allocation=response(
            ipalloc_html(
                [
                    (IP, f" {HOLDER} ", "renamed-host", "off"),
                    (IP, other_holder, "allocation-only", "off"),
                ]
            )
        ),
    )
    result = preflight.inspect_allocation_conflicts(router, REQUESTS)

    assert result.conflicts == [
        preflight.AllocationConflict(IP, TARGET, HOLDER, "device-name", "on"),
        preflight.AllocationConflict(IP, TARGET, other_holder, "allocation-only", "off"),
    ]


@pytest.mark.parametrize("source", ["devices", "ipalloc"])
def test_rescan_refuses_a_persistent_holder_visible_in_either_source(clock, source):
    held = [(IP, HOLDER, "still-online", "on")]
    router = Router(
        response(devices_html(held)),
        response(ipalloc_html(held if source == "ipalloc" else [])),
        device_states=(response(devices_html(held if source == "devices" else [])),),
        clock=clock,
    )
    with pytest.raises(UsageError, match=HOLDER):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert clock.now == 180
    assert len(router.posts) == 1


def test_rescan_waits_until_both_sources_clear_the_original_holder(clock):
    held = [(IP, HOLDER, "old", "off")]
    router = Router(
        response(devices_html(held)),
        response(ipalloc_html()),
        device_states=(response(devices_html(held)), response(devices_html())),
        clock=clock,
    )
    preflight.rescan_allocation_conflicts(
        router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
    )
    assert clock.sleeps == [60, 5]
    assert len(router.posts) == 1


@pytest.mark.parametrize("source", ["devices", "ipalloc"])
def test_rescan_rechecks_pending_ips_that_were_initially_clear(clock, source):
    second_ip = "192.168.1.65"
    second_target = "02:0a:0b:0c:0d:04"
    second_holder = "02:0a:0b:0c:0d:05"
    new_conflict = [(second_ip, second_holder, "new-owner", "on")]
    requests = [*REQUESTS, SnapshotReservation(second_target, second_ip)]
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html(new_conflict if source == "ipalloc" else [])),
        device_states=(response(devices_html(new_conflict if source == "devices" else [])),),
        clock=clock,
    )
    with pytest.raises(UsageError) as error:
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, requests), log=lambda _: None
        )
    assert second_ip in str(error.value) and second_holder in str(error.value)
    assert second_target in str(error.value)
    assert len(router.posts) == 1


@pytest.mark.parametrize("read_duration", [120, 125], ids=["at-deadline", "after-deadline"])
def test_rescan_starts_no_second_page_read_after_first_read_reaches_deadline(clock, read_duration):
    class SlowDevicesRouter(Router):
        def get_cgi_page(self, page):
            result = super().get_cgi_page(page)
            if self.posts and page == "devices":
                clock.now += read_duration
            return result

    router = SlowDevicesRouter(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html()),
        device_states=(response(devices_html()),),
        clock=clock,
    )
    with pytest.raises(UsageError, match=HOLDER):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert router.read_starts == [("devices", 60)]
    assert len(router.posts) == 1


@pytest.mark.parametrize(
    "bad",
    [
        response("<title>Login</title>"),
        response("<html>Processing</html>"),
        response(devices_html(), 503),
        response(devices_html([(IP, "Unknown", "old", "on")])),
        response(
            devices_html().replace("</table>", f"<tr><td>on</td><td>{IP} / missing-mac</td><td></td></tr></table>")
        ),
        RuntimeError("read timed out"),
    ],
    ids=["login", "invalid-html", "http-error", "invalid-mac", "incomplete-row", "transport-error"],
)
def test_unreadable_refreshed_devices_fails_even_when_allocation_is_clear(clock, bad):
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html()),
        device_states=(bad,),
        clock=clock,
    )
    with pytest.raises(UsageError, match="[Vv]alidat|[Rr]ead|ownership"):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert len(router.posts) == 1


@pytest.mark.parametrize(
    "bad",
    [
        response("<title>Login</title>"),
        response("<html>Processing</html>"),
        response(ipalloc_html(), 503),
        response(ipalloc_html([(IP, "Unknown", "old", "off")])),
        response(ipalloc_html().replace("</table>", "<tr><td>Processing</td></tr></table>")),
        RuntimeError("read timed out"),
    ],
    ids=["login", "invalid-html", "http-error", "invalid-mac", "incomplete-row", "transport-error"],
)
def test_unreadable_initial_allocation_fails_even_when_devices_is_clear(bad):
    router = Router(response(devices_html()), initial_allocation=bad)
    with pytest.raises(UsageError, match="[Vv]alidat|[Rr]ead|ownership"):
        preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert router.posts == []


def test_two_argument_preflight_still_rechecks_its_conflicts_in_both_sources(clock):
    conflict = preflight.AllocationConflict(IP, TARGET, HOLDER, "old", "off")
    pending = preflight.AllocationPreflight([conflict], {"Clear": "Clear & Rescan"})
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")])), response(ipalloc_html()), clock=clock)
    with pytest.raises(UsageError, match=HOLDER):
        preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert clock.now == 180


def test_final_read_started_before_deadline_may_finish_after_deadline(clock):
    class SlowAllocationRouter(Router):
        def get_cgi_page(self, page):
            result = super().get_cgi_page(page)
            if self.posts and page == "ipalloc":
                clock.now += 125
            return result

    router = SlowAllocationRouter(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html()),
        device_states=(response(devices_html()),),
        clock=clock,
    )
    preflight.rescan_allocation_conflicts(
        router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
    )
    assert router.read_starts == [("devices", 60), ("ipalloc", 60)]
    assert clock.now == 185
    assert len(router.posts) == 1


@pytest.mark.parametrize("page", ["devices", "ipalloc"])
@pytest.mark.parametrize("error_type", ["connection", "auth", "pool", "http"])
def test_initial_typed_router_errors_preserve_identity_before_any_write(page, error_type):
    from bgwcli.client import RouterResponseError
    from bgwcli.errors import RouterAuthError, RouterConnectionError, RouterSessionPoolFullError

    types = {
        "connection": RouterConnectionError, "auth": RouterAuthError, "pool": RouterSessionPoolFullError,
        "http": RouterResponseError,
    }
    failure = types[error_type]("synthetic failure")
    router = Router(
        failure if page == "devices" else response(devices_html()),
        initial_allocation=failure if page == "ipalloc" else None,
    )
    with pytest.raises(types[error_type]) as caught:
        preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert caught.value is failure
    assert router.posts == []


@pytest.mark.parametrize("page", ["devices", "ipalloc"])
@pytest.mark.parametrize("bad", [response("<html>Processing</html>"), response("busy", 503)])
def test_rescan_retries_unverifiable_poll_then_proves_both_sources_clear(clock, page, bad):
    held = response(devices_html([(IP, HOLDER, "old", "off")]))
    router = Router(
        held,
        *((bad, response(ipalloc_html())) if page == "ipalloc" else (response(ipalloc_html()),)),
        device_states=(bad, response(devices_html())) if page == "devices" else (response(devices_html()),),
        clock=clock,
    )
    preflight.rescan_allocation_conflicts(
        router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
    )
    assert len(router.posts) == 1
    assert clock.now == 65
    assert router.read_starts[-2:] == [("devices", 65), ("ipalloc", 65)]


def test_persistent_unverifiable_polls_exhaust_budget_without_claiming_free(clock):
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response("<html>Processing</html>"),
        device_states=(response(devices_html()),),
        clock=clock,
    )
    with pytest.raises(UsageError, match="[Uu]nverifiable|[Vv]erification|[Vv]alidat") as caught:
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert "after Clear" in str(caught.value)
    assert clock.now == 180
    assert all(start < 180 for _, start in router.read_starts)
    assert len(router.posts) == 1


@pytest.mark.parametrize("duplicate_in_devices", [False, True])
def test_fixed_owner_is_preserved_in_dedup_and_refused_without_clear(clock, duplicate_in_devices):
    held = [(IP, HOLDER, "holder", "off")]
    router = Router(
        response(devices_html(held if duplicate_in_devices else [])),
        initial_allocation=response(ipalloc_html(held).replace("DHCP Allocation", "Fixed Allocation")),
    )
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert pending.conflicts[0].fixed_allocation is True
    with pytest.raises(UsageError, match="[Ff]ixed.*manual|manual.*[Ff]ixed") as caught:
        preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert IP in str(caught.value) and HOLDER in str(caught.value)
    assert router.posts == []
    assert clock.now == 0


@pytest.mark.parametrize("error_type", ["connection", "http"])
def test_rescan_retries_typed_transient_read_failures_without_another_clear(clock, error_type):
    from bgwcli.client import RouterResponseError
    from bgwcli.errors import RouterConnectionError

    types = {"connection": RouterConnectionError, "http": RouterResponseError}
    failure = types[error_type]("busy")
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        failure,
        response(ipalloc_html()),
        device_states=(response(devices_html()),),
        clock=clock,
    )
    preflight.rescan_allocation_conflicts(
        router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
    )
    assert router.read_times == [60, 65]
    assert len(router.posts) == 1


def test_devices_static_label_is_not_evidence_of_a_fixed_allocation(clock):
    devices = key_value_devices_html(
        [
            ("MAC Address", HOLDER),
            ("IPv4 Address / Name", f"{IP} / stale"),
            ("Status", "off"),
            ("Allocation", "static"),
        ]
    )
    router = Router(response(devices), response(ipalloc_html()), device_states=(response(devices_html()),))
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    assert not pending.conflicts[0].fixed_allocation
    preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert len(router.posts) == 1
    assert clock.now == 60


def test_fixed_owner_newly_visible_after_clear_stops_polling_promptly(clock):
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html([(IP, HOLDER, "fixed", "off")]).replace("DHCP Allocation", "Fixed Allocation")),
        device_states=(response(devices_html()),),
        clock=clock,
    )
    with pytest.raises(UsageError, match="manual"):
        preflight.rescan_allocation_conflicts(
            router, preflight.inspect_allocation_conflicts(router, REQUESTS), log=lambda _: None
        )
    assert clock.now == 60
    assert router.read_times == [60]
    assert len(router.posts) == 1


def test_persistent_rescan_pool_failure_preserves_coordination_metadata(clock):
    from bgwcli.errors import RouterSessionPoolFullError

    error = RouterSessionPoolFullError("pool busy")
    error.waited_ms, error.retry_count = 1234, 3
    held = [(IP, HOLDER, "old", "off")]
    router = Router(response(devices_html(held)), device_states=(error,), clock=clock)
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    with pytest.raises(RouterSessionPoolFullError, match="unverifiable") as caught:
        preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert caught.value.waited_ms == 1234
    assert caught.value.retry_count == 3
    # A full pool ends the rescan at once: polling on would only compete for the exhausted pool.
    assert len(router.posts) == 1 and clock.now == 60 and router.read_starts == [("devices", 60)]


def test_pool_full_in_the_rescan_poll_is_kept_even_when_a_later_read_would_succeed(clock):
    """A full session pool is never a disposable ownership snapshot: the rescan stops on it (no further
    polls) and raises it with its metadata, so the coordinator records the cooldown even though the
    next read would have proved the address free."""
    from bgwcli.errors import RouterSessionPoolFullError

    pool = RouterSessionPoolFullError("pool busy")
    pool.waited_ms, pool.retry_count = 1234, 3
    router = Router(
        response(devices_html([(IP, HOLDER, "old", "off")])),
        response(ipalloc_html()),
        device_states=(pool, response(devices_html())),
        clock=clock,
    )
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    with pytest.raises(RouterSessionPoolFullError, match="unverifiable") as caught:
        preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert caught.value is pool
    assert (caught.value.waited_ms, caught.value.retry_count) == (1234, 3)
    assert IP in str(caught.value) and HOLDER in str(caught.value)
    assert len(router.posts) == 1
    assert router.read_starts == [("devices", 60)]


@pytest.mark.parametrize("later", ["connection", "http", "usage"])
def test_pool_full_seen_in_the_rescan_poll_is_not_swallowed_by_a_later_different_error(clock, later):
    """A full session pool must reach the coordinator (cooldown) even when a later poll failed
    differently: otherwise the next run would hammer the exhausted pool at once."""
    from bgwcli.client import RouterResponseError
    from bgwcli.errors import RouterConnectionError, RouterSessionPoolFullError

    pool = RouterSessionPoolFullError("pool busy")
    pool.waited_ms, pool.retry_count = 1234, 3
    other = {"connection": RouterConnectionError("reset"), "http": RouterResponseError("HTTP 500"),
             "usage": UsageError("bad page")}[later]
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")])), device_states=(pool, other), clock=clock)
    pending = preflight.inspect_allocation_conflicts(router, REQUESTS)
    with pytest.raises(RouterSessionPoolFullError, match="unverifiable") as caught:
        preflight.rescan_allocation_conflicts(router, pending, log=lambda _: None)
    assert (caught.value.waited_ms, caught.value.retry_count) == (1234, 3)
    assert len(router.posts) == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_rescan_timeout_keeps_pool_metadata_and_coordinator_cooldown(clock, tmp_env, monkeypatch, automatic):
    import json

    from test_autorestore import Fetcher, make_dump, reset_pages

    from bgwcli.autorestore import AutorestoreOptions, result_output, run_autorestore
    from bgwcli.client import BGW320Client, session_pool_full_error
    from bgwcli.errors import RouterSessionPoolFullError
    from bgwcli.session import SessionCoordinatorOptions, session_paths, with_router_session

    error = session_pool_full_error(waited_ms=4321, retry_count=4)
    router = Router(response(devices_html([(IP, HOLDER, "old", "off")])), device_states=(error,), clock=clock)
    client = BGW320Client("synthetic.invalid", transport=lambda _request: pytest.fail("no network expected"))
    monkeypatch.setattr(client, "get_cgi_page", router.get_cgi_page)
    monkeypatch.setattr(client, "post_cgi_page", router.post_cgi_page)
    options = SessionCoordinatorOptions(cache_ttl_ms=60000, pool_cooldown_ms=60000,
                                        lock_timeout_ms=1000, wait_for_session=False)

    def work():
        if automatic:
            return run_autorestore(
                lambda: (client, False), make_dump(), AutorestoreOptions(commit=True),
                fetch_pages=Fetcher(reset_pages()), log=lambda _: None,
            )
        pending = preflight.inspect_allocation_conflicts(client, REQUESTS)
        return preflight.rescan_allocation_conflicts(client, pending, log=lambda _: None)

    if automatic:
        output = result_output(with_router_session(client, options, work))
        assert output["sessionPoolFull"] is True
        assert [output["waitedMs"], output["retryCount"]] == [4321, 4]
        assert output["allocationPreflight"]["error"]["waitedMs"] == 4321
    else:
        with pytest.raises(RouterSessionPoolFullError) as caught:
            with_router_session(client, options, work)
        assert [caught.value.waited_ms, caught.value.retry_count] == [4321, 4]
    cooldown = session_paths(client.session_identity()).cooldown
    deadline = json.loads(cooldown.read_text())["until"]
    before = len(router.gets)
    with pytest.raises(RouterSessionPoolFullError, match="cooldown"):
        with_router_session(client, options, work)
    assert len(router.gets) == before and len(router.posts) == 1
    assert json.loads(cooldown.read_text())["until"] == deadline
