"""A write re-sent once after a Login-page answer says so on every failure shape of the generic
Save-page paths too: `writeAttempts` is present and the guidance says "sent 2 times", never "once"."""

import json
from urllib.parse import urlsplit

import pytest
from save_helpers import ERROR, NO_CHANGE, SAVED_RED, client_with, form, html

from bgwcli import cli, diagnostics

LOGIN = '<title>Login</title><form><input name="nonce" value="abc123"><input name="password"></form>'
POOL_FULL_PAGE = "<title>Login</title><p>All web server sessions are in use</p>"
DIAG_FORM = (
    '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="WebAddress" value="">'
    '<input type="submit" name="Ping" value="Ping"></form>'
)
# A Save-less page: its write is answered by the POST itself (inline banner or redirect target).
NO_SAVE_FORM = (
    '<form action="/cgi-bin/ipalloc.ha"><input name="nonce" value="abc123">'
    '<input type="text" name="setting" value="old">'
    '<input type="submit" name="Other" value="Other"></form>'
)


def _wire(page, *, resend, after, first_form=None, answer=None):
    """`after` is what every GET answers once the write went out: 'old' (acknowledgement never
    appears), 'saved', 'rejected', 'lost' (session-wide refusal), 'pool-full' or 'fail-500'."""
    state = {"posts": 0, "wrote": 0}

    def handler(request, _n):
        path = urlsplit(request.url).path
        if path.endswith("login.ha"):
            if request.method == "POST":
                if after == "lost" and state["wrote"]:
                    return html(LOGIN)  # the gateway refuses the automatic re-login
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            state["wrote"] = 1
            return answer(page) if answer else html("", 302, {"location": f"/cgi-bin/{page}.ha"})
        if not state["wrote"]:
            return html(first_form or form(page, "old"))
        if after == "saved":
            return html(form(page, "new", banner=SAVED_RED))
        if after == "rejected":
            return html(form(page, "new", banner=ERROR))
        if after == "lost":
            return html(LOGIN)
        if after == "pool-full":
            return html(POOL_FULL_PAGE)
        if after == "fail-500":
            return html("boom", 500)
        return html(first_form or form(page, "old"))

    return handler


def _run(monkeypatch, capsys, argv, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    monkeypatch.setattr(diagnostics, "_sleep", lambda s: None)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = sum(1 for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha"))
    return code, out, posts


def _text(out):
    return out.get("warning") or out.get("error") or ""


SET_ETHERLAN = ["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN"]
SUBMIT_ETHERLAN = ["submit", "etherlan", "Save", "setting=new", "--commit", "--confirm", "ETHERLAN"]


@pytest.mark.parametrize("resend", [True, False])
def test_ack_timeout_after_a_resend_says_sent_two_times(tmp_env, clock, monkeypatch, capsys, resend):
    code, out, posts = _run(monkeypatch, capsys, SET_ETHERLAN, _wire("etherlan", resend=resend, after="old"))
    assert code == 2 and posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in _text(out) and "sent once" not in _text(out)
    else:
        assert "writeAttempts" not in out
        assert "sent once" in _text(out)


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("fault", ["lost", "pool-full"])
def test_session_wide_failure_during_the_ack_read_after_a_resend(
    tmp_env, clock, monkeypatch, capsys, fault, resend
):
    code, out, posts = _run(monkeypatch, capsys, SET_ETHERLAN, _wire("etherlan", resend=resend, after=fault))
    assert code == 2 and posts == (2 if resend else 1)
    assert out["ok"] is False and out["writeAttempted"] is True
    assert out.get("sessionPoolFull") is (True if fault == "pool-full" else None)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in out["error"] and "sent once" not in out["error"]
    else:
        assert "writeAttempts" not in out
        assert "sent once" in out["error"]


@pytest.mark.parametrize("resend", [True, False])
def test_submit_rejection_banner_after_a_resend(tmp_env, clock, monkeypatch, capsys, resend):
    code, out, posts = _run(monkeypatch, capsys, SUBMIT_ETHERLAN, _wire("etherlan", resend=resend, after="rejected"))
    assert code == 1 and posts == (2 if resend else 1)
    assert out["outcome"] == "failed" and "router rejected the change" in _text(out)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in _text(out) and "sent once" not in _text(out)
    else:
        assert "writeAttempts" not in out


@pytest.mark.parametrize("resend", [True, False])
def test_save_less_page_redirect_banner_after_a_resend(tmp_env, clock, monkeypatch, capsys, resend):
    argv = ["submit", "ipalloc", "Other", "--commit", "--confirm", "IPALLOC"]
    code, out, posts = _run(
        monkeypatch, capsys, argv, _wire("ipalloc", resend=resend, after="rejected", first_form=NO_SAVE_FORM)
    )
    assert code == 1 and posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
    else:
        assert "writeAttempts" not in out


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("fault", ["fail-500", "lost", "pool-full"])
def test_save_less_page_unreadable_answer_after_a_resend(tmp_env, clock, monkeypatch, capsys, fault, resend):
    argv = ["submit", "ipalloc", "Other", "--commit", "--confirm", "IPALLOC"]
    code, out, posts = _run(
        monkeypatch, capsys, argv, _wire("ipalloc", resend=resend, after=fault, first_form=NO_SAVE_FORM)
    )
    assert code == 2 and posts == (2 if resend else 1)
    text = _text(out)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in text and "sent once" not in text
    else:
        assert "writeAttempts" not in out
        assert "sent once" in text


def _diag_wire(*, resend, after_post):
    state = {"posts": 0, "wrote": 0}

    def handler(request, _n):
        if request.url.endswith("login.ha"):
            return html("", 302, {"location": "/cgi-bin/home.ha"}) if request.method == "POST" else html(LOGIN)
        if request.method == "POST":
            state["posts"] += 1
            if resend and state["posts"] == 1:
                return html(LOGIN)
            state["wrote"] = 1
            return html("", 302, {"location": "/cgi-bin/diag.ha"})
        if not state["wrote"]:
            return html(DIAG_FORM)
        return after_post()

    return handler


DIAG_ARGV = ["diagnostics", "ping", "example.com", "--commit", "--confirm", "DIAG"]


@pytest.mark.parametrize("resend", [True, False])
def test_diagnostic_resend_success_reports_write_attempts(tmp_env, clock, monkeypatch, capsys, resend):
    result_page = (
        '<form action="/cgi-bin/diag.ha"><input name="nonce" value="abc123">'
        '<pre id="r">PING ok 64 bytes</pre></form>'
    )
    code, out, posts = _run(
        monkeypatch, capsys, DIAG_ARGV, _diag_wire(resend=resend, after_post=lambda: html(result_page))
    )
    assert code == 0 and out["committed"] is True and posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
    else:
        assert "writeAttempts" not in out


@pytest.mark.parametrize("resend", [True, False])
def test_diagnostic_unread_result_after_a_resend_says_started_two_times(
    tmp_env, clock, monkeypatch, capsys, resend
):
    reads = {"n": 0}

    def after_post():
        # The redirect target read for the banner answers an empty page; the result poll then fails.
        reads["n"] += 1
        return html(DIAG_FORM) if reads["n"] == 1 else html("boom", 500)

    code, out, posts = _run(monkeypatch, capsys, DIAG_ARGV, _diag_wire(resend=resend, after_post=after_post))
    assert code == 2 and posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
        assert "started 2 times" in _text(out) and "started once" not in _text(out)
    else:
        assert "writeAttempts" not in out
        assert "started once" in _text(out)


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("fault", ["lost", "pool-full"])
def test_diagnostic_poll_raising_after_a_resend(tmp_env, clock, monkeypatch, capsys, fault, resend):
    after = (lambda: html(LOGIN, 403)) if fault == "lost" else (lambda: html(POOL_FULL_PAGE))
    code, out, posts = _run(monkeypatch, capsys, DIAG_ARGV, _diag_wire(resend=resend, after_post=after))
    assert code == 2 and out["ok"] is False and posts == (2 if resend else 1)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in out["error"] and "sent once" not in out["error"]
    else:
        assert "writeAttempts" not in out
        assert "sent once" in out["error"]


# --- the Wi-Fi Warning Continue confirmation and the verification stage ---------------------------------

WARN_PAGE = (
    '<form action="/cgi-bin/{page}.ha"><input name="nonce" value="abc123">'
    '<input type="submit" name="Continue" value="Continue"></form>'
)
PLEASE_WAIT = "<html><body>Please wait</body></html>"


def _flex(page, *, save_resend, warn=False, continue_modes=(), after=(), first_form=None):
    """A scripted gateway. The Save POST answers Login first when `save_resend`, then 302 (to the Wi-Fi
    Warning page when `warn`). `continue_modes` scripts the Continue POSTs ('login', 'ok', '500', the last
    one repeating); `after` scripts the page GETs once the write went out ('saved', 'old', 'nochange',
    'wait', 'pool-full', 'plain'; the last one repeating)."""
    state = {"saves": 0, "continues": 0, "gets": 0, "wrote": False}

    def handler(request, _n):
        path = urlsplit(request.url).path
        if path.endswith("login.ha"):
            if request.method == "POST" and not ("lost" in after and state["wrote"]):
                return html("", 302, {"location": "/cgi-bin/home.ha"})
            return html(LOGIN)  # a lost session: the gateway refuses the automatic re-login
        if request.method == "POST":
            if b"Continue" in request.body:
                mode = continue_modes[min(state["continues"], len(continue_modes) - 1)]
                state["continues"] += 1
                if mode == "login":
                    return html(LOGIN)
                if mode == "500":
                    return html("boom", 500)
                state["wrote"] = True
                return html("", 302, {"location": f"/cgi-bin/{page}.ha"})
            state["saves"] += 1
            if save_resend and state["saves"] == 1:
                return html(LOGIN)
            if warn:
                return html("", 302, {"location": "/cgi-bin/wifiwarn_advanced.ha"})
            state["wrote"] = True
            return html("", 302, {"location": f"/cgi-bin/{page}.ha"})
        if path.endswith("wifiwarn_advanced.ha"):
            return html(WARN_PAGE.format(page=page))
        if not state["wrote"]:
            return html(first_form or form(page, "old"))
        mode = after[min(state["gets"], len(after) - 1)]
        state["gets"] += 1
        return {
            "saved": lambda: html(form(page, "new", banner=SAVED_RED)),
            "old": lambda: html(form(page, "old")),
            "nochange": lambda: html(form(page, "old", banner=NO_CHANGE)),
            "wait": lambda: html(PLEASE_WAIT),
            "pool-full": lambda: html(POOL_FULL_PAGE),
            "lost": lambda: html(LOGIN),
            "plain": lambda: html(first_form or form(page, "new")),
        }[mode]()

    return handler


def _config_post_count(wire):
    posts = [r for r in wire.requests if r.method == "POST" and not urlsplit(r.url).path.endswith("login.ha")]
    return len(posts)


@pytest.mark.parametrize("page,token", [("wconfig", "WCONFIG"), ("etherlan", "ETHERLAN")])
@pytest.mark.parametrize(
    "save_resend,continue_modes,after,total",
    [
        (True, ("500",), ("old",), 3),  # re-sent Save, single Continue fails after being sent
        (True, ("ok",), ("old",), 3),  # re-sent Save, Continue ok, acknowledgement times out
        (False, ("login", "500"), ("old",), 3),  # single Save, re-sent Continue fails
        (False, ("login", "ok"), ("old",), 3),  # single Save, re-sent Continue, acknowledgement times out
    ],
)
def test_warning_confirmation_reports_the_larger_post_count(
    tmp_env, clock, monkeypatch, capsys, page, token, save_resend, continue_modes, after, total
):
    client, wire = client_with(
        _flex(page, save_resend=save_resend, warn=True, continue_modes=continue_modes, after=after)
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", page, "setting=new", "--commit", "--confirm", token, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and _config_post_count(wire) == total
    assert out["writeAttempts"] == 2
    assert "sent 2 times" in _text(out) and "sent once" not in _text(out)


def test_a_continue_answered_with_the_login_page_is_re_sent_and_reports_two_attempts(
    tmp_env, clock, monkeypatch, capsys
):
    # Save -> Warning -> Continue answered with the Login page -> re-login -> Continue re-sent -> saved.
    client, wire = client_with(
        _flex("wconfig", save_resend=False, warn=True, continue_modes=("login", "ok"), after=("saved",))
    )
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", "wconfig", "setting=new", "--commit", "--confirm", "WCONFIG", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["committed"] is True and out["acknowledgementObserved"] is True
    assert _config_post_count(wire) == 3, "Save, Continue, re-sent Continue"
    assert out["writeAttempts"] == 2


@pytest.mark.parametrize("page,token", [("wconfig", "WCONFIG"), ("etherlan", "ETHERLAN")])
@pytest.mark.parametrize("continue_modes", [("500",), ("ok",)])
def test_warning_confirmation_with_single_posts_keeps_the_once_text(
    tmp_env, clock, monkeypatch, capsys, page, token, continue_modes
):
    client, wire = client_with(_flex(page, save_resend=False, warn=True, continue_modes=continue_modes, after=("old",)))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", page, "setting=new", "--commit", "--confirm", token, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and _config_post_count(wire) == 2
    # A FAILED write reports the transport's own count (existing contract, also 1 for a lone POST);
    # only a successful single POST omits the key.
    if continue_modes == ("ok",):
        assert "writeAttempts" not in out
    else:
        assert out["writeAttempts"] == 1
    assert "2 times" not in _text(out)


def _verify_run(monkeypatch, capsys, page, token, handler, verb="set"):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main([verb, page, "setting=new", "--commit", "--confirm", token, "--json"])
    out = json.loads(capsys.readouterr().out)
    return code, out, _config_post_count(wire)


@pytest.mark.parametrize("resend", [True, False])
def test_unreadable_verification_after_a_resend_says_sent_two_times(tmp_env, clock, monkeypatch, capsys, resend):
    code, out, posts = _verify_run(
        monkeypatch, capsys, "etherlan", "ETHERLAN", _flex("etherlan", save_resend=resend, after=("saved", "wait"))
    )
    assert code == 2 and posts == (2 if resend else 1) and out["committed"] is True
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in _text(out) and "sent once" not in _text(out)
    else:
        assert "writeAttempts" not in out
        assert "sent once" in _text(out)


@pytest.mark.parametrize("resend", [True, False])
def test_no_changes_detected_mismatch_after_a_resend_says_sent_two_times(
    tmp_env, clock, monkeypatch, capsys, resend
):
    code, out, posts = _verify_run(
        monkeypatch, capsys, "etherlan", "ETHERLAN", _flex("etherlan", save_resend=resend, after=("nochange",))
    )
    assert code == 1 and posts == (2 if resend else 1)
    assert "normalised the change" in _text(out)
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in _text(out) and "sent once" not in _text(out)
    else:
        assert "writeAttempts" not in out
        assert "sent once" in _text(out)


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("page,token,first_form,after", [
    ("etherlan", "ETHERLAN", None, ("saved", "pool-full")),  # Save page: the step carries the count
    ("ipalloc", "IPALLOC", NO_SAVE_FORM, ("plain", "pool-full")),  # Save-less page: no step at all
])
def test_pool_full_during_verification_after_a_resend_carries_write_attempts(
    tmp_env, clock, monkeypatch, capsys, resend, page, token, first_form, after
):
    code, out, posts = _verify_run(
        monkeypatch, capsys, page, token,
        _flex(page, save_resend=resend, after=after, first_form=first_form),
    )
    assert code == 2 and out["ok"] is False and out["sessionPoolFull"] is True
    assert posts == (2 if resend else 1)
    if page == "ipalloc":
        # Save-less page: the POST was answered before the verification read failed; nothing else was observed.
        assert out["writeAttempted"] is True and out["writeResponseReceived"] is True
        for key in ("committed", "writePerformed", "acknowledgementObserved"):
            assert key not in out
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in out["error"] and "sent once" not in out["error"]
        assert "verify the gateway state before retrying" in out["error"]
    else:
        assert "writeAttempts" not in out
        assert "sent once" in out["error"]
        assert "verify the gateway state before retrying" in out["error"]


@pytest.mark.parametrize("resend", [True, False])
@pytest.mark.parametrize("fault", ["pool-full", "lost"])
def test_session_fault_in_verification_after_an_acknowledged_save_carries_guidance(
    tmp_env, clock, monkeypatch, capsys, resend, fault
):
    code, out, posts = _verify_run(
        monkeypatch, capsys, "etherlan", "ETHERLAN", _flex("etherlan", save_resend=resend, after=("saved", fault))
    )
    assert code == 2 and posts == (2 if resend else 1)
    assert out["committed"] is True and out["acknowledgementObserved"] is True
    assert out.get("sessionPoolFull") is (True if fault == "pool-full" else None)
    text = _text(out)  # a lost session is retried into a warning result; a full pool raises
    if fault == "pool-full":
        assert "Changes saved was observed" in out["error"]
    assert "verify the gateway state before retrying" in text
    if resend:
        assert out["writeAttempts"] == 2
        assert "sent 2 times" in text and "sent once" not in text
    else:
        assert "writeAttempts" not in out
        assert "sent once" in text


def test_pool_full_in_verification_after_a_resend_prints_guidance_in_default_mode(
    tmp_env, clock, monkeypatch, capsys
):
    client, _wire_log = client_with(_flex("etherlan", save_resend=True, after=("saved", "pool-full")))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "sleep", lambda s: None)
    code = cli.main(["set", "etherlan", "setting=new", "--commit", "--confirm", "ETHERLAN"])
    captured = capsys.readouterr()
    assert code == 2
    assert "sent 2 times" in captured.err and "verify the gateway state before retrying" in captured.err
