"""Every restore configuration mutation must finish its acknowledgement phase."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from bgwcli import restore
from bgwcli.errors import RouterConnectionError
from bgwcli.restore import RestoreStep, execute_restore
from bgwcli.types import HttpResponse

SAVED_RED = '<div id="error-message-text" style="color: red">Changes saved</div>'
PENDING = "<html><body>Processing configuration</body></html>"
ERROR = (
    '<img id="error-message-icon" src="/images/icon_error.png">'
    '<div id="error-message-text">A required setting is empty</div>'
)
MUTATIONS = [
    ("dosprotect", "form", {"flood_protect": "on", "Save": "Save"}),
    ("services", "add-service", {"Service": "Example", "Add": "Add"}),
    ("services", "remove-service", {"Remove_1": "Remove"}),
    ("apphosting", "add-forward", {"service": "Example", "device": "02:0a:0b:0c:0d:02", "Add": "Add"}),
    ("apphosting", "remove-forward", {"Remove_1": "Remove"}),
]
MUTATION_IDS = ["form-save", "service-add", "service-remove", "forward-add", "forward-remove"]


def response(page: str, body: str = "", status: int = 200, location: str | None = None) -> HttpResponse:
    return HttpResponse(
        status,
        "OK" if status == 200 else "Found",
        {"Location": location or f"/cgi-bin/{page}.ha"} if status == 302 else {},
        body,
        f"https://synthetic.invalid/cgi-bin/{page}.ha",
    )


def steps(mutation: tuple[str, str, dict[str, str]]) -> list[RestoreStep]:
    page, kind, payload = mutation
    return [
        RestoreStep(1, kind, page, "first mutation", raw_payload=payload),
        RestoreStep(2, "form", "etherlan", "later mutation", raw_payload={"mode": "auto", "Save": "Save"}),
    ]


class MutationRouter:
    """Emulate only HTTP responses; execution and banner parsing remain production code."""

    def __init__(self, confirmations: Sequence[str], *, post_status: int = 302, post_body: str = "") -> None:
        self.confirmations = confirmations
        self.post_status = post_status
        self.post_body = post_body
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.polls = 0
        self.events: list[str] = []

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse:
        self.posts.append((page, dict(fields)))
        self.events.append("first-post" if len(self.posts) == 1 else "later-post")
        if len(self.posts) == 1:
            return response(page, self.post_body, self.post_status)
        return response(page, SAVED_RED)

    def get_cgi_page(self, page: str) -> HttpResponse:
        self.events.append("confirmation-get")
        count = self.polls
        self.polls += 1
        assert count < 20, "confirmation polling exceeded its bounded fake-time window"
        assert self.confirmations, "a complete POST acknowledgement should not require another GET"
        return response(page, self.confirmations[min(count, len(self.confirmations) - 1)])


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
    monkeypatch.setattr(restore, "monotonic", fake.monotonic, raising=False)
    monkeypatch.setattr(restore, "sleep", fake.sleep, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_TIMEOUT_SECONDS", 3.0, raising=False)
    monkeypatch.setattr(restore, "SAVE_CONFIRMATION_POLL_SECONDS", 1.0, raising=False)
    return fake


@pytest.mark.parametrize("mutation", MUTATIONS, ids=MUTATION_IDS)
@pytest.mark.parametrize("post_status", [200, 302])
def test_mutation_polls_changes_saved_before_the_next_write(clock, mutation, post_status):
    router = MutationRouter([PENDING, SAVED_RED], post_status=post_status, post_body=PENDING)
    execution = execute_restore(router, steps(mutation))

    assert [result.status for result in execution.steps] == ["applied", "applied"]
    assert execution.stopped_at is None
    assert router.events[:4] == ["first-post", "confirmation-get", "confirmation-get", "later-post"]
    assert clock.sleeps


@pytest.mark.parametrize("mutation", MUTATIONS, ids=MUTATION_IDS)
def test_missing_save_acknowledgement_times_out_without_resend_or_later_write(clock, mutation):
    router = MutationRouter([PENDING])
    execution = execute_restore(router, steps(mutation))

    assert [result.status for result in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert execution.steps[0].error
    assert router.posts == [(mutation[0], mutation[2])]
    assert router.polls > 1
    assert clock.now >= 3.0


@pytest.mark.parametrize("mutation", MUTATIONS, ids=MUTATION_IDS)
def test_http_200_router_error_stops_without_polling_or_later_write(clock, mutation):
    router = MutationRouter([SAVED_RED], post_status=200, post_body=ERROR)
    execution = execute_restore(router, steps(mutation))

    assert [result.status for result in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert "required setting" in (execution.steps[0].error or "")
    assert router.posts == [(mutation[0], mutation[2])]
    assert router.polls == 0
    assert clock.sleeps == []


def test_red_styled_changes_saved_in_http_200_body_is_positive(clock):
    router = MutationRouter([], post_status=200, post_body=SAVED_RED)
    execution = execute_restore(router, steps(MUTATIONS[0]))

    assert [result.status for result in execution.steps] == ["applied", "applied"]
    assert execution.stopped_at is None
    assert router.polls == 0
    assert clock.sleeps == []


def test_transient_read_timeout_retries_confirmation_without_reposting(clock):
    class BusyRouter(MutationRouter):
        def get_cgi_page(self, page):
            if not self.polls:
                self.polls += 1
                raise RouterConnectionError("busy while saving")
            return super().get_cgi_page(page)

    router = BusyRouter([SAVED_RED])
    execution = execute_restore(router, steps(MUTATIONS[0]))

    assert [r.status for r in execution.steps] == ["applied", "applied"]
    assert len(router.posts) == 2  # One POST per planned step.
    assert router.polls == 2
    assert clock.sleeps


def test_acknowledgement_is_read_from_the_redirect_target(clock):
    class RedirectRouter(MutationRouter):
        def post_cgi_page(self, page, fields):
            self.posts.append((page, dict(fields)))
            return response(page, status=302, location="/cgi-bin/saveconfirmation.ha")

        def get_cgi_page(self, page):
            assert page == "saveconfirmation"
            return response(page, SAVED_RED)

    router = RedirectRouter([])
    execution = execute_restore(router, steps(MUTATIONS[0])[:1])

    assert execution.steps[0].status == "applied"
    assert len(router.posts) == 1


class WifiRouter:
    def __init__(self, confirmations: Sequence[str]) -> None:
        self.confirmations = confirmations
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.events: list[str] = []
        self.polls = 0

    def post_cgi_page(self, page: str, fields: Mapping[str, str]) -> HttpResponse:
        self.posts.append((page, dict(fields)))
        if "Continue" in fields:
            self.events.append("continue-post")
            return response("wconfig", status=302)
        if page == "wconfig":
            self.events.append("save-post")
            return response("wconfig", status=302, location="/cgi-bin/wifiwarn_advanced.ha")
        self.events.append("later-post")
        return response(page, SAVED_RED)

    def post_form(self, nonce_page: str, post_path: str, fields: Mapping[str, str]) -> HttpResponse:
        assert nonce_page == "wifiwarn_advanced"  # Continue's nonce is the warning page's own
        return self.post_cgi_page(post_path.removesuffix(".ha"), fields)

    def get_cgi_page(self, page: str) -> HttpResponse:
        if page == "wifiwarn_advanced":
            self.events.append("warning-get")
            return response(
                page,
                '<html><body><form method="post" action="/cgi-bin/wconfig.ha">'
                '<input type="submit" name="Continue" value="Continue">'
                '</form></body></html>',
            )
        self.events.append("confirmation-get")
        count = self.polls
        self.polls += 1
        assert count < 20, "Wi-Fi acknowledgement polling exceeded its timeout"
        return response(page, self.confirmations[min(count, len(self.confirmations) - 1)])


def test_wifi_continue_waits_for_final_changes_saved_before_later_write(clock):
    router = WifiRouter([PENDING, SAVED_RED])
    execution = execute_restore(router, steps(("wconfig", "form", {"key11": "synthetic", "Save": "Save..."})))

    assert [result.status for result in execution.steps] == ["applied", "applied"]
    assert router.events[:6] == [
        "save-post", "warning-get", "continue-post", "confirmation-get", "confirmation-get", "later-post"
    ]
    assert router.posts[1] == ("wconfig", {"Continue": "Continue"})
    assert clock.sleeps


def test_wifi_continue_timeout_never_reposts_continue_or_runs_later_write(clock):
    router = WifiRouter([PENDING])
    execution = execute_restore(router, steps(("wconfig", "form", {"key11": "synthetic", "Save": "Save..."})))

    assert [result.status for result in execution.steps] == ["failed", "not-run"]
    assert execution.stopped_at == 1
    assert router.posts == [
        ("wconfig", {"key11": "synthetic", "Save": "Save..."}),
        ("wconfig", {"Continue": "Continue"}),
    ]
    assert router.polls > 1
    assert clock.now >= 3.0
