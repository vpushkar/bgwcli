"""A button that only opens an editor saves nothing: `opened`, committed false, exit 0."""

import pytest
from integration_html import IPALLOC_HTML
from opener_helpers import MAC, PACKET_FILTER, run_cli
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.actions import ROUTER_ACTIONS

EDITOR = (
    '<html><body><form method="post" action="/cgi-bin/packetfilter.ha">'
    '<input type="hidden" name="nonce" value="ab12"><input type="submit" name="Save" value="Save"></form></body></html>'
)


def test_the_opener_flag_marks_the_packet_filter_add_rule_actions():
    assert {a.name for a in ROUTER_ACTIONS if a.opener} == {
        "packet-filter-add-drop-rule", "packet-filter-add-pass-rule",
    }


@pytest.mark.parametrize("name", ["packet-filter-add-drop-rule", "packet-filter-add-pass-rule"])
def test_add_rule_action_only_opens_the_editor(tmp_env, clock, monkeypatch, capsys, name):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/packetfilter.ha"})
        return html(EDITOR)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["action", name, "--commit", "--confirm", "PACKETFILTER"], handler
    )
    assert code == 0 and len(posts) == 1
    assert out["committed"] is False and out["outcome"] == "opened"
    assert out["writeAttempted"] is True and "nothing was saved" in out["result"]
    assert out["acknowledgementObserved"] is False and out["writePerformed"] is False


def test_submit_allocate_only_opens_the_entry_editor(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/ipalloc.ha"})
        return html(IPALLOC_HTML)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["submit", "ipalloc", f"Allocate_{MAC}", "--commit", "--confirm", "IPALLOC"], handler
    )
    assert code == 0 and len(posts) == 1
    assert out["committed"] is False and out["outcome"] == "opened"
    assert "nothing was saved" in out["result"]
    assert out["acknowledgementObserved"] is False and out["writePerformed"] is False


TWO_ALLOCATE = (
    '<form action="/cgi-bin/ipalloc.ha"><input type="hidden" name="nonce" value="ab12">'
    f'<input type="submit" name="Allocate_{MAC}" value="Allocate">'
    '<input type="submit" name="Allocate_02:0a:0b:0c:0d:03" value="Allocate"></form>'
)


def test_an_ambiguous_button_label_is_a_usage_error_naming_the_candidates(tmp_env, clock, monkeypatch, capsys):
    client, wire = client_with(lambda r, n: html(TWO_ALLOCATE))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["submit", "ipalloc", "Allocate", "--commit", "--confirm", "IPALLOC", "--json"])
    captured = capsys.readouterr()
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    assert code == 1 and posts == []
    text = captured.out + captured.err
    assert f"Allocate_{MAC}" in text and "Allocate_02:0a:0b:0c:0d:03" in text


def test_an_exact_button_name_among_equal_labels_still_opens(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/ipalloc.ha"})
        return html(TWO_ALLOCATE)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["submit", "ipalloc", "Allocate_02:0a:0b:0c:0d:03", "--commit", "--confirm", "IPALLOC"],
        handler,
    )
    assert code == 0 and len(posts) == 1 and out["outcome"] == "opened"


@pytest.mark.parametrize("token", ["AddDropRule", "Add a 'Drop' Rule"])
def test_submit_packetfilter_add_rule_is_an_opener_by_name_or_label(tmp_env, clock, monkeypatch, capsys, token):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/packetfilter.ha"})
        return html(PACKET_FILTER)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["submit", "packetfilter", token, "--commit", "--confirm", "PACKETFILTER"], handler
    )
    assert code == 0 and len(posts) == 1
    assert out["committed"] is False and out["outcome"] == "opened"
    assert out["acknowledgementObserved"] is False


def test_submit_allocate_by_label_is_an_opener(tmp_env, clock, monkeypatch, capsys):
    page = TWO_ALLOCATE.replace('value="Allocate">', 'value="Other">', 1)

    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/ipalloc.ha"})
        return html(page)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["submit", "ipalloc", "Other", "--commit", "--confirm", "IPALLOC"], handler
    )
    assert code == 0 and len(posts) == 1 and out["outcome"] == "opened"
