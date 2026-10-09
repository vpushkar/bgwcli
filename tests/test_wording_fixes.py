"""Message-wording fixes: each assertion pins the new text and that the outcome (exit code, committed,
POST count, exception type, evidence flags) is unchanged."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from save_helpers import NO_CHANGE, client_with, form, html
from test_autorestore import FakeRouter, Fetcher, make_dump, reset_pages
from test_autorestore_deadline import Clock, _factory
from test_save_contract import WARNING_WITH_CONTINUE

from bgwcli import autorestore, cli
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.client import NoncePageRefusedError
from bgwcli.errors import UsageError
from bgwcli.recovery_state import RecoveryCheckpoint
from bgwcli.restore import CONTINUE_NOT_POSTED, VERIFY_BEFORE_RETRYING, RestoreExecution, RestoreStepResult
from bgwcli.save_result import decide_save_result

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
NO_NONCE_FORM = '<form action="/cgi-bin/dosprotect.ha"><input type="text" name="setting" value="old"></form>'


# --- R6-8: the Login-body re-send refused after the first POST was answered with the Login page ------


def test_login_body_resend_refused_says_first_post_was_answered_with_the_login_page():
    state = {"gets": 0, "posts": 0}

    def handler(request, _n):
        path = urlsplit(request.url).path
        if path.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            return html(LOGIN)
        state["gets"] += 1
        return html(form("dosprotect", "old") if state["gets"] == 1 else NO_NONCE_FORM)

    client, _transport = client_with(handler)
    with pytest.raises(NoncePageRefusedError) as caught:
        client.post_cgi_page("dosprotect", {"setting": "new"})
    message = str(caught.value)
    assert state["posts"] == 1, "only the first POST went out; the re-send was refused before any POST"
    assert "nothing was sent" not in message
    assert "the first POST was answered with the Login page" in message
    assert "the re-send was refused" in message
    assert "no write nonce found for dosprotect.ha" in message


def test_login_body_resend_refused_through_the_cli_is_an_unconfirmed_failed_write(
    clock, tmp_env, capsys, monkeypatch
):
    def handler(request, _n):
        path = urlsplit(request.url).path
        if path.endswith("login.ha"):
            if request.method == "POST":
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            return html(LOGIN)
        gets = sum(1 for r in wire.requests if r.method == "GET" and urlsplit(r.url).path.endswith("dosprotect.ha"))
        # the set read and the nonce read see the form; the re-read after the Login answer has no nonce
        return html(form("dosprotect", "old") if gets <= 2 else NO_NONCE_FORM)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", "dosprotect", "setting=new", "--commit", "--confirm", "DOSPROTECT", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 2
    assert output["committed"] is False and output["outcome"] == "failed"
    assert output["writeAttempted"] is True
    assert "writePerformed" not in output
    assert "the first POST was answered with the Login page" in output["warning"]
    config_posts = [r for r in wire.requests if r.method == "POST" and urlsplit(r.url).path.endswith("dosprotect.ha")]
    assert len(config_posts) == 1, "only the first POST went out; the re-send was refused before any POST"


# --- R7-A2: autorestore reason for a refused Continue after an answered Save --------------------------


def _continue_refused_step(error: str) -> RestoreStepResult:
    return RestoreStepResult(
        order=1, page="wconfig", kind="form", description="save wconfig", status="failed",
        status_code=302, write_attempted=True, write_response_received=True, error=error,
        error_type="NoncePageRefusedError",
    )


def _run_one_failed_step(monkeypatch, tmp_path, step):
    monkeypatch.setattr(
        autorestore, "execute_restore", lambda client, steps, on_step=None: RestoreExecution(steps=[step], stopped_at=1)
    )
    store = RecoveryCheckpoint("router.local", make_dump(), None, root=tmp_path / "recovery")
    return run_autorestore(
        lambda: (FakeRouter(), False), make_dump(), AutorestoreOptions(commit=True, max_passes=3, wait_seconds=1),
        fetch_pages=Fetcher(reset_pages(), reset_pages(), reset_pages()), sleep=lambda _: None,
        log=lambda _: None, checkpoint=store,
    )


def test_refused_continue_after_an_answered_save_does_not_say_got_no_answer(monkeypatch, tmp_path):
    step = _continue_refused_step(
        f"wconfig: no write nonce found for wconfig.ha. {CONTINUE_NOT_POSTED}, so the change is not confirmed"
    )
    result = _run_one_failed_step(monkeypatch, tmp_path, step)
    assert result.status == "error" and result.exit_code == 2 and result.write_unanswered is True
    assert "got no answer" not in result.reason
    assert "the Save was answered and the Continue was not posted" in result.reason


def test_an_unanswered_write_without_a_continue_refusal_still_says_got_no_answer(monkeypatch, tmp_path):
    step = RestoreStepResult(
        order=1, page="wconfig", kind="form", description="save wconfig", status="failed",
        write_attempted=True, write_response_received=False, error="timed out", error_type="RouterConnectionError",
    )
    result = _run_one_failed_step(monkeypatch, tmp_path, step)
    assert result.status == "error" and result.exit_code == 2 and result.write_unanswered is True
    assert "got no answer" in result.reason


# --- R7-B3: Wi-Fi Warning, Continue POST failed after the Save was answered 302 -----------------------


def _continue_failing_flow(fail):
    def handle(request, _n):
        path = request.url.rsplit("/", 1)[-1]
        body = request.body.decode() if isinstance(request.body, bytes) else str(request.body or "")
        if request.method == "POST":
            if "Continue" in body:
                return fail(request)
            return html("", status=302, headers={"location": "/cgi-bin/wifiwarn_advanced.ha"})
        if path.startswith("wifiwarn"):
            return html(WARNING_WITH_CONTINUE.format(page="wconfig"))
        return html(form("wconfig", "old", "setting"))

    return handle


def _run_wifi(monkeypatch, capsys, fail):
    client, wire = client_with(_continue_failing_flow(fail))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG", "--json"])
    output = json.loads(capsys.readouterr().out)
    return code, output, sum(r.method == "POST" for r in wire.requests)


def test_continue_http_error_warning_names_both_posts(clock, tmp_env, capsys, monkeypatch):
    code, output, posts = _run_wifi(monkeypatch, capsys, lambda r: html("<html>Error</html>", status=500))
    assert code == 2 and output["committed"] is False and output["outcome"] == "failed"
    assert posts == 2 and output["statusCode"] == 302
    assert output["writeAttempted"] is True
    warning = output["warning"]
    assert "Save POST was answered 302" in warning
    assert "Continue POST failed or got no answer" in warning
    assert "Two POSTs went out" in warning
    assert "sent once" not in warning and "Continue was not posted" not in warning


def test_continue_transport_fault_warning_names_both_posts(clock, tmp_env, capsys, monkeypatch):
    from bgwcli.errors import RouterConnectionError

    def fail(request):
        raise RouterConnectionError("connection reset")

    code, output, posts = _run_wifi(monkeypatch, capsys, fail)
    assert code == 2 and output["committed"] is False and output["statusCode"] == 302
    assert "Save POST was answered 302" in output["warning"]
    assert "Continue POST failed or got no answer" in output["warning"]
    assert "sent once" not in output["warning"]
    assert posts == 2, "the Save POST and the failed Continue POST"


# --- C10-4: legacy "<unchecked>" text-field skip warning -----------------------------------------------


def test_legacy_unchecked_skip_warning_says_only_that_field_is_left_out_of_the_save():
    from test_unchecked_text_roundtrip import UNCHECKED, _capture, _pages, _plan, _round_trip, field

    from bgwcli.dumpfile import read_dump_file

    _, snap = _capture([field("key11", "text", UNCHECKED), field("label", "text", "Home")])
    with tempfile.TemporaryDirectory() as tmp:
        _, path = _round_trip(snap, Path(tmp))
        data = json.loads(path.read_text())
        data.pop("formUncheckedText")
        path.write_text(json.dumps(data))
        legacy = read_dump_file(path)
    _, steps = _plan(legacy, _pages([field("key11", "text", "Other"), field("label", "text", "Away")]))
    skips = [s for s in steps if s.kind == "skip"]
    warning = skips[0].warning
    assert "that field is left out of the save and keeps its live value" in warning
    assert "the step is skipped" not in warning and "was not changed" not in warning
    assert "key11" in warning and UNCHECKED not in warning and "Other" not in warning
    # the rest of the plan is unaffected: one skip for the field, one form save restoring the other field
    assert [(s.kind, s.page) for s in steps] == [("skip", "dosprotect"), ("form", "dosprotect")]
    form_step = steps[1]
    assert form_step.blocked is None and form_step.raw_payload["label"] == "Home"
    # the skipped field is never posted with the marker text; it carries its live value
    assert form_step.raw_payload["key11"] == "Other"
    assert UNCHECKED not in form_step.raw_payload.values()


# --- B11-m1: no duplicated guidance sentence on the no-change + unreadable re-read warning ------------


def test_no_change_with_a_warning_that_already_ends_with_the_guidance_is_not_doubled():
    decision = decide_save_result(
        "unchanged", None, False, None, requested=True, write_attempted=True,
        verify_warning=f"could not read the page back; {VERIFY_BEFORE_RETRYING}",
    )
    assert (decision.exit_code, decision.outcome, decision.committed) == (2, "failed", False)
    assert decision.message.count(VERIFY_BEFORE_RETRYING) == 1
    assert ".;" not in decision.message and decision.message.endswith(VERIFY_BEFORE_RETRYING)


def test_no_change_with_a_plain_warning_still_gets_the_guidance_once():
    decision = decide_save_result(
        "unchanged", None, False, None, requested=True, write_attempted=True, verify_warning="HTTP 500",
    )
    assert (decision.exit_code, decision.outcome, decision.committed) == (2, "failed", False)
    assert decision.message.endswith(f"HTTP 500; {VERIFY_BEFORE_RETRYING}")
    assert decision.message.count(VERIFY_BEFORE_RETRYING) == 1


# --- B11-m2: no-change answer, then the verification re-read hits a full pool --------------------------


def test_no_change_then_pool_full_on_the_re_read_mentions_the_no_change_answer(clock, tmp_env, capsys, monkeypatch):
    from bgwcli.client import session_pool_full_error

    state = {"posted": False}

    def handle(request, _n):
        if request.method == "POST":
            state["posted"] = True
            return html(form("etherlan", "old", banner=NO_CHANGE))
        if state["posted"]:
            raise session_pool_full_error()
        return html(form("etherlan", "old"))

    client, wire = client_with(handle)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["ok"] is False
    assert out.get("committed") is not True
    assert sum(r.method == "POST" for r in wire.requests) == 1
    assert "No changes detected was observed" in out["error"]
    assert "The change was sent once" in out["error"] and "Changes saved was observed" not in out["error"]


# --- R9-B-m2: ipalloc refusal names the real flow -------------------------------------------------------

IPALLOC_BUTTONS = (
    '<form action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="abc123">'
    '<input type="submit" name="Allocate_1" value="Allocate"></form>'
)


def test_set_on_ipalloc_with_no_open_editor_names_the_allocate_then_set_flow(clock, tmp_env, capsys, monkeypatch):
    client, wire = client_with(lambda r, n: html(IPALLOC_BUTTONS))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["set", "ipalloc", "alloc_aa:bb:cc:dd:ee:ff=192.168.1.50", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["errorType"] == UsageError.__name__
    assert not any(r.method == "POST" for r in wire.requests)
    assert "submit ipalloc Allocate_<n>" in out["error"] and "set ipalloc alloc_<mac>=<ip>" in out["error"]
    assert "no settable fields" in out["error"]


# --- R12-X1 wording: run limit before the next restore step on pass N > 1 -------------------------------


def test_first_pass_run_limit_says_before_the_first_restore_step(tmp_path):
    clock = Clock()
    router = FakeRouter()
    from test_autorestore_deadline import _slow_begin_store

    store = _slow_begin_store(tmp_path, clock, 2.0, begun=False)
    fetcher = Fetcher(reset_pages())
    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_run_seconds=1800.0),
        fetch_pages=lambda c, p: (setattr(clock, "now", 1799.0), fetcher(c, p))[1],
        sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [] and result.status == "not-converged" and result.exit_code == 1
    assert "before the first restore step" in result.reason and "nothing was sent" in result.reason


def test_later_pass_run_limit_says_before_the_next_restore_step(tmp_path, monkeypatch):
    clock = Clock()
    router = FakeRouter()
    begins = {"n": 0}

    class SlowSecondBegin(RecoveryCheckpoint):
        def begin(self):
            super().begin()
            begins["n"] += 1
            if begins["n"] == 2:
                clock.now += 2.0

    store = SlowSecondBegin("router.local", make_dump(), None, root=tmp_path / "recovery")

    def execute(client, steps, on_step=None):
        step = RestoreStepResult(order=1, page="dosprotect", kind="form", description="x", status="blocked",
                                 write_attempted=False)
        return RestoreExecution(steps=[step], stopped_at=None)

    monkeypatch.setattr(autorestore, "execute_restore", execute)
    fetcher = Fetcher(reset_pages(), reset_pages(), reset_pages(), reset_pages())

    def fetch(c, p):
        if begins["n"] >= 1:
            clock.now = 1799.0  # the closing read of pass 1; pass 2's checkpoint begin then crosses the limit
        return fetcher(c, p)

    result = run_autorestore(
        _factory(router), make_dump(), AutorestoreOptions(commit=True, max_passes=3, max_run_seconds=1800.0),
        fetch_pages=fetch, sleep=lambda _: None, log=lambda _: None, monotonic=clock, checkpoint=store,
    )
    assert router.posts == [] and result.status == "not-converged" and result.exit_code == 1
    assert "before the next restore step" in result.reason and "nothing was sent" in result.reason
    assert "before the first" not in result.reason
