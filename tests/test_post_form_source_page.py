"""The fresh page read inside the write POST is validated before the POST is built: a Please-wait,
Login, Page-not-found or parser-cut page, a page without a form control, or a page that yields no
nonce is a structural failure (exit 2) with nothing sent, on every write path."""

import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.errors import BgwError

FORM = (
    '<html><head><title>DoS</title></head><body><form method="post" action="/cgi-bin/dosprotect.ha">'
    '<input type="hidden" name="nonce" value="abcd01"><select name="flood_protect">'
    '<option value="on" selected>On</option><option value="off">Off</option></select>'
    '<input type="submit" name="Save" value="Save"></form></body></html>'
)
WAIT = "<html><head><title>Please wait</title></head><body>Please wait...</body></html>"
NOT_FOUND = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"
NO_NONCE = FORM.replace('<input type="hidden" name="nonce" value="abcd01">', "")
LOGIN = (
    '<html><head><title>Login</title></head><body><form method="post" action="/cgi-bin/login.ha">'
    '<input type="hidden" name="nonce" value="zz99"><input type="password" name="password">'
    '<input type="submit" name="Continue" value="Continue"></form></body></html>'
)
OTHER_FORMS = (
    '<html><body><form action="/cgi-bin/crestart.ha?1"><input name="nonce" value="aaaa01">'
    '<input type="submit" name="Broadband" value="Restart"></form>'
    '<form action="/cgi-bin/wrestart.ha?1"><input name="nonce" value="bbbb01">'
    '<input type="submit" name="WRestart1" value="Restart"></form></body></html>'
)
TWO_FORMS_NO_NONCE_FOR_TARGET = FORM.replace(
    '<input type="hidden" name="nonce" value="abcd01">', ""
).replace("</body>", '<form action="/cgi-bin/other.ha"><input name="nonce" value="oo11"></form></body>')

BAD_SECOND_READS = [
    pytest.param(WAIT, id="please-wait"),
    pytest.param(NOT_FOUND, id="page-not-found"),
    pytest.param(NO_NONCE, id="no-nonce"),
    pytest.param(TWO_FORMS_NO_NONCE_FOR_TARGET, id="several-forms-no-target-nonce"),
]


def _run(monkeypatch, capsys, second_read, argv):
    state = {"gets": 0}

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/dosprotect.ha"})
        state["gets"] += 1
        return html(FORM if state["gets"] == 1 else second_read)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--commit", "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


@pytest.mark.parametrize("second_read", BAD_SECOND_READS)
def test_set_refuses_an_unusable_source_page_before_any_post(tmp_env, clock, monkeypatch, capsys, second_read):
    code, out, posts = _run(
        monkeypatch, capsys, second_read, ["set", "dosprotect", "flood_protect=off", "--confirm", "DOSPROTECT"]
    )
    assert posts == []
    assert code == 2, json.dumps(out)
    assert out["writeAttempted"] is False
    assert out["writePerformed"] is False
    assert out.get("committed") is not True


@pytest.mark.parametrize("second_read", BAD_SECOND_READS)
def test_submit_refuses_an_unusable_source_page_before_any_post(tmp_env, clock, monkeypatch, capsys, second_read):
    code, out, posts = _run(
        monkeypatch, capsys, second_read, ["submit", "dosprotect", "Save", "--confirm", "DOSPROTECT"]
    )
    assert posts == []
    assert code == 2, json.dumps(out)
    assert out["writeAttempted"] is False
    assert out["writePerformed"] is False


@pytest.mark.parametrize(
    "body", [WAIT, NOT_FOUND, NO_NONCE, OTHER_FORMS], ids=["wait", "not-found", "no-nonce", "other-forms"]
)
def test_post_form_refuses_before_transport(tmp_env, clock, body):
    client, wire = client_with(lambda r, n: html(body))
    with pytest.raises(BgwError):
        client.post_form("home", "wrestart.ha?9", {"WRestart9": "Restart"})
    assert [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")] == []


def test_post_form_refuses_a_login_page_source(tmp_env, clock):
    client, wire = client_with(lambda r, n: html(LOGIN))
    with pytest.raises(BgwError):
        client.post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    assert [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")] == []


def test_post_form_refuses_a_parser_cut_source_page(tmp_env, clock, monkeypatch):
    from bgwcli import client as client_module
    from bgwcli import parser

    real = parser.parse_page

    def cut(page, body, **kwargs):
        parsed = real(page, body, **kwargs)
        parsed.truncated = True
        return parsed

    monkeypatch.setattr(parser, "parse_page", cut)
    del client_module
    client, wire = client_with(lambda r, n: html(FORM))
    with pytest.raises(BgwError):
        client.post_cgi_page("dosprotect", {"flood_protect": "off", "Save": "Save"})
    assert [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")] == []


def test_a_good_source_page_still_posts_with_its_nonce(tmp_env, clock):
    def handler(request, n):
        return html("", 302) if request.method == "POST" else html(OTHER_FORMS)

    client, wire = client_with(handler)
    client.post_form("home", "wrestart.ha?1", {"WRestart1": "Restart"})
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert len(posts) == 1 and b"nonce=bbbb01" in posts[0].body
