"""Allocation saves must be confirmed before any subsequent restore mutation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from bgwcli import restore
from bgwcli.restore import RestoreFollowUp, RestoreStep, execute_restore
from bgwcli.types import HttpResponse

MAC_A = "02:0a:0b:0c:0d:02"
MAC_B = "02:0a:0b:0c:0d:03"
IP_A = "192.168.1.64"
IP_B = "192.168.1.70"
SAVED = '<div id="error-message-text">Changes saved</div>'
ERROR = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">The selected address is unavailable</div>'
)


def allocation_html(mac: str = MAC_A, ip: str = IP_A, *, banner: str = "") -> str:
    return (
        f"<html><body>{banner}<table>"
        "<tr><th>IPv4 Address / Name</th><th>MAC Address</th>"
        "<th>Status</th><th>Allocation</th></tr>"
        f"<tr><td>{ip}</td><td>{mac}</td><td>on</td><td>Fixed Allocation</td></tr>"
        "</table></body></html>"
    )


def entry_html(mac: str, ip: str) -> str:
    return (
        '<html><body><form action="/cgi-bin/ipalloc.ha" method="post">'
        f'<select name="alloc_{mac}"><option value="normal">Address from DHCP pool</option>'
        f'<option value="{ip}">{ip}</option></select>'
        '<input type="submit" name="Save" value="Save"></form></body></html>'
    )


def reserve_step(order: int, mac: str, ip: str) -> RestoreStep:
    return RestoreStep(
        order=order,
        kind="reserve",
        page="ipalloc",
        description=f"reserve {ip} for {mac}",
        button=f"Allocate_{mac}",
        raw_payload={f"Allocate_{mac}": "Allocate"},
        follow_up=RestoreFollowUp("ipalloc", f"alloc_{mac}", ip, "Save"),
    )


def response(body: str = "", status: int = 200) -> HttpResponse:
    return HttpResponse(
        status,
        "OK" if status == 200 else "Found",
        {"Location": "/cgi-bin/ipalloc.ha"} if status == 302 else {},
        body,
        "https://synthetic.invalid/cgi-bin/ipalloc.ha",
    )


class AllocationRouter:
    """Only the network boundary is fake; entry/result HTML uses the real parser."""

    def __init__(
        self,
        confirmations: Mapping[str, Sequence[str]],
        *,
        save_status: int = 302,
        save_body: str = "",
    ) -> None:
        self.confirmations = confirmations
        self.save_status = save_status
        self.save_body = save_body
        self.target: tuple[str, str] | None = None
        self.saving = False
        self.polls: dict[str, int] = {}
        self.posts: list[dict[str, str]] = []
        self.events: list[tuple[str, str]] = []

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse:
        assert page == "ipalloc"
        self.posts.append(dict(fields))
        if "Save" in fields:
            assert self.target is not None
            self.saving = True
            self.events.append(("save", self.target[0]))
            return response(self.save_body, self.save_status)
        key = next(iter(fields))
        mac = key.removeprefix("Allocate_")
        self.target = (mac, IP_A if mac == MAC_A else IP_B)
        self.saving = False
        self.events.append(("allocate", mac))
        return response(status=302)

    def get_cgi_page(self, page: str) -> HttpResponse:
        assert page == "ipalloc"
        assert self.target is not None
        mac, ip = self.target
        if not self.saving:
            self.events.append(("entry", mac))
            return response(entry_html(mac, ip))
        self.events.append(("confirmation", mac))
        count = self.polls.get(mac, 0)
        self.polls[mac] = count + 1
        assert count < 20, "confirmation polling did not obey its timeout"
        states = self.confirmations[mac]
        assert states, "a complete HTTP 200 Save response should need no confirmation GET"
        return response(states[min(count, len(states) - 1)])


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def __init__(self) -> None:
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            assert seconds > 0
            self.sleeps.append(seconds)
            self.now += seconds

    fake = Clock()
    # raising=False lets these behavior tests run RED before production adds the seams.
    monkeypatch.setattr(restore, "monotonic", fake.monotonic, raising=False)
    monkeypatch.setattr(restore, "sleep", fake.sleep, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_TIMEOUT_SECONDS", 3.0, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_POLL_SECONDS", 1.0, raising=False)
    return fake


def test_each_reservation_is_confirmed_before_the_next_allocate(clock):
    router = AllocationRouter(
        {
            MAC_A: [allocation_html(ip="192.168.1.90"), allocation_html(banner=SAVED)],
            MAC_B: [allocation_html(MAC_B, IP_B), allocation_html(MAC_B, IP_B, banner=SAVED)],
        }
    )
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A), reserve_step(2, MAC_B, IP_B)])

    assert [step.status for step in execution.steps] == ["applied", "applied"]
    assert execution.stopped_at is None
    assert router.polls[MAC_A] >= 2
    assert router.polls[MAC_B] >= 2
    assert router.events.index(("allocate", MAC_B)) > max(
        i for i, event in enumerate(router.events) if event == ("confirmation", MAC_A)
    )
    assert router.posts == [
        {f"Allocate_{MAC_A}": "Allocate"},
        {f"alloc_{MAC_A}": IP_A, "Save": "Save"},
        {f"Allocate_{MAC_B}": "Allocate"},
        {f"alloc_{MAC_B}": IP_B, "Save": "Save"},
    ]
    assert clock.sleeps


def test_delayed_confirmation_must_match_both_target_mac_and_saved_ip(clock):
    router = AllocationRouter(
        {
            MAC_A: [
                allocation_html(MAC_B, IP_A, banner=SAVED),
                allocation_html(MAC_A, IP_B, banner=SAVED),
                allocation_html(banner=SAVED),
            ]
        }
    )
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A)])

    assert execution.steps[0].status == "applied"
    assert router.polls[MAC_A] >= 3
    assert clock.sleeps


@pytest.mark.parametrize(
    "incomplete",
    [
        SAVED,
        allocation_html(),
        allocation_html(ip=IP_B, banner=SAVED),
        allocation_html(mac=MAC_B, banner=SAVED),
    ],
    ids=["banner-without-row", "row-without-banner", "wrong-ip", "wrong-mac"],
)
def test_timeout_stops_later_allocations_without_retrying_save(clock, incomplete):
    router = AllocationRouter({MAC_A: [incomplete], MAC_B: [allocation_html(MAC_B, IP_B, banner=SAVED)]})
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A), reserve_step(2, MAC_B, IP_B)])

    assert [step.status for step in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert "Timed out" in (execution.steps[0].error or "")
    assert router.posts == [{f"Allocate_{MAC_A}": "Allocate"}, {f"alloc_{MAC_A}": IP_A, "Save": "Save"}]
    assert router.polls[MAC_A] > 1
    assert clock.now >= 3.0


def test_complete_http_200_save_body_confirms_without_losing_the_banner(clock):
    router = AllocationRouter({MAC_A: []}, save_status=200, save_body=allocation_html(banner=SAVED))
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A)])

    assert execution.steps[0].status == "applied"
    assert execution.stopped_at is None
    assert router.polls == {}
    assert clock.sleeps == []


def test_save_acknowledgement_is_remembered_while_the_allocation_table_catches_up(clock):
    router = AllocationRouter(
        {MAC_A: [allocation_html(ip=IP_B), allocation_html()]},
        save_status=200,
        save_body=SAVED,
    )
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A)])

    assert execution.steps[0].status == "applied"
    assert router.polls[MAC_A] == 2
    assert len(router.posts) == 2  # Allocate and Save, each once.


def test_http_200_save_body_without_confirmation_still_polls_and_times_out(clock):
    router = AllocationRouter({MAC_A: [allocation_html()]}, save_status=200, save_body=allocation_html())
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A), reserve_step(2, MAC_B, IP_B)])

    assert [step.status for step in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert router.polls[MAC_A] > 1
    assert clock.now >= 3.0


@pytest.mark.parametrize("save_status", [200, 302], ids=["save-body", "confirmation-get"])
def test_router_error_fails_immediately_and_stops_later_steps(clock, save_status):
    router = AllocationRouter({MAC_A: [ERROR]}, save_status=save_status, save_body=ERROR if save_status == 200 else "")
    execution = execute_restore(router, [reserve_step(1, MAC_A, IP_A), reserve_step(2, MAC_B, IP_B)])

    assert [step.status for step in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert "unavailable" in (execution.steps[0].error or "")
    assert router.posts == [{f"Allocate_{MAC_A}": "Allocate"}, {f"alloc_{MAC_A}": IP_A, "Save": "Save"}]
    assert router.polls.get(MAC_A, 0) <= 1
    assert clock.sleeps == []
