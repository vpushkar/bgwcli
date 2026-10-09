"""`set` and `submit` whose answered POST is followed only by cut pages keep the write evidence.

A body cut by the parser's bounds on the first read after an answered POST (the POST's own 200 body, or the
first poll after a 302) is an unreadable poll exactly like a Please-wait page: the wait polls to the
deadline and reports exit 2 with writeAttempted / writeResponseReceived true and no acknowledgement. It
must never escape the wait as a bare TruncatedPageError that hides the fact the change was sent. One POST."""

from __future__ import annotations

import json

import pytest
from integration_html import entry_page_html
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli import parser as parser_module

MAC = "02:0a:0b:0c:0d:02"
IP = "192.168.1.64"
# The element bound is lowered for this module so a small body is cut; the real 150000-element
# bound is exercised in test_parser_bounds.py and test_truncated_consumers.py.
TEST_MAX_ELEMENTS = 1000
CUT = "<html><body>" + "<p>x</p>" * (TEST_MAX_ELEMENTS + 100) + "</body></html>"


@pytest.fixture(autouse=True)
def small_element_bound(monkeypatch):
    monkeypatch.setattr(parser_module, "MAX_ELEMENTS", TEST_MAX_ELEMENTS)
PLEASE_WAIT = "<html><head><title>Please wait</title></head><body>Loading configuration...</body></html>"
EVIDENCE = ("writeAttempted", "writeResponseReceived", "acknowledgementObserved", "committed", "outcome", "error")

DOSPROTECT = (
    '<html><head><title>Firewall Advanced</title></head><body><h1>Firewall Advanced</h1>'
    '<form method="post" action="/cgi-bin/dosprotect.ha"><input type="hidden" name="nonce" value="ab12">'
    '<select name="icmp_downstream_echo_rqst_drop_wan"><option value="off" selected>off</option>'
    '<option value="on">on</option></select><input type="submit" name="Save" value="Save"></form></body></html>'
)
IPALLOC = entry_page_html(MAC, [IP]).replace('name="nonce" value="n"', 'name="nonce" value="ab12"')


def _run(monkeypatch, capsys, clock, argv, entry_page, *, answer, polls, redirect):
    """One commit whose POST is answered by `answer` (a 302 to the page when `redirect`) and whose every
    later read is `polls` (a body, or the first-poll body followed by the rest)."""
    state = {"posted": False, "polled": 0}

    def handler(request, _n):
        if request.method == "POST":
            state["posted"] = True
            if redirect:
                return html("", 302, {"location": request.url.rsplit("/", 1)[-1]})
            return html(answer)
        if not state["posted"]:
            return html(entry_page)
        state["polled"] += 1
        return html(polls)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


SET_DOS = ["set", "dosprotect", "icmp_downstream_echo_rqst_drop_wan=on", "--confirm", "DOSPROTECT"]
SUBMIT_ALLOC = ["submit", "ipalloc", "Save", f"alloc_{MAC}={IP}", "--confirm", "IPALLOC"]


@pytest.mark.parametrize(
    ("argv", "entry_page"), [(SET_DOS, DOSPROTECT), (SUBMIT_ALLOC, IPALLOC)], ids=["set-dosprotect", "submit-ipalloc"]
)
@pytest.mark.parametrize("redirect", [False, True], ids=["cut-answer-body", "cut-first-poll-after-302"])
def test_cut_pages_after_an_answered_post_carry_the_write_evidence_like_please_wait(
    monkeypatch, capsys, clock, tmp_env, argv, entry_page, redirect
):
    code, out, posts = _run(monkeypatch, capsys, clock, argv, entry_page, answer=CUT, polls=CUT, redirect=redirect)
    wait_code, wait_out, wait_posts = _run(
        monkeypatch, capsys, clock, argv, entry_page, answer=PLEASE_WAIT, polls=PLEASE_WAIT, redirect=redirect
    )
    assert code == 2 and len(posts) == 1
    assert out["writeAttempted"] is True and out["writeResponseReceived"] is True
    assert out["acknowledgementObserved"] is False
    assert out["committed"] is False and out["outcome"] == "failed"
    assert "errorType" not in out
    assert (wait_code, len(wait_posts)) == (code, len(posts))
    assert {k: out.get(k) for k in EVIDENCE} == {k: wait_out.get(k) for k in EVIDENCE}
    # The whole payload (warning text included) is identical: a cut body is exactly a Please-wait body.
    assert out == wait_out


STATE_CLAUSE = "the requested state could not be read"


def test_unreadable_state_clause_only_where_the_wait_tracks_a_state(monkeypatch, capsys, clock, tmp_env):
    _, dos, _ = _run(monkeypatch, capsys, clock, SET_DOS, DOSPROTECT, answer=CUT, polls=CUT, redirect=False)
    assert STATE_CLAUSE not in dos["warning"]
    _, alloc, _ = _run(monkeypatch, capsys, clock, SUBMIT_ALLOC, IPALLOC, answer=CUT, polls=CUT, redirect=False)
    assert STATE_CLAUSE in alloc["warning"]
