"""An opener button only opens an editor: `submit` refuses to merge KEY=VALUE assignments into its POST."""

import pytest
from integration_html import IPALLOC_HTML
from opener_helpers import MAC, PACKET_FILTER, run_cli
from page_builders import button, field, page
from save_helpers import client_with, html

from bgwcli.errors import UsageError
from bgwcli.mutations import build_submit_plan
from bgwcli.parser import parse_page

ALLOC = f"alloc_{MAC}=1.2.3.4"


def test_build_submit_plan_refuses_assignments_on_an_ipalloc_opener():
    parsed = parse_page("ipalloc", IPALLOC_HTML, include_secrets=True)
    with pytest.raises(UsageError) as info:
        build_submit_plan("ipalloc", parsed, f"Allocate_{MAC}", [ALLOC])
    text = str(info.value)
    assert "only opens an editor on ipalloc" in text and "set ipalloc" in text and "Nothing was posted" in text
    assert f"`submit ipalloc Allocate_{MAC} --commit --confirm IPALLOC`" in text
    assert "`set ipalloc KEY=VALUE --commit --confirm IPALLOC`" in text
    assert "<token>" not in text


def test_build_submit_plan_still_builds_an_opener_without_assignments():
    parsed = parse_page("ipalloc", IPALLOC_HTML, include_secrets=True)
    plan = build_submit_plan("ipalloc", parsed, f"Allocate_{MAC}", [])
    assert plan.button is not None and plan.button.name == f"Allocate_{MAC}"


def test_build_submit_plan_refuses_assignments_on_a_packet_filter_add_rule_opener():
    with_opener = page(
        "packetfilter", fields=[field("name", "text", "x")], buttons=[button("AddDropRule", "Add a 'Drop' Rule")]
    )
    with pytest.raises(UsageError, match="only opens an editor on packetfilter") as info:
        build_submit_plan("packetfilter", with_opener, "AddDropRule", ["name=y"])
    assert "set packetfilter" in str(info.value) and "Nothing was posted" in str(info.value)
    assert "--confirm PACKETFILTER" in str(info.value)
    assert build_submit_plan("packetfilter", with_opener, "AddDropRule", []).button is not None


def test_build_submit_plan_keeps_assignments_on_a_non_opener_button():
    with_save = page("diag", fields=[field("Address", "text", "")], buttons=[button("Ping", "Ping")])
    assert build_submit_plan("diag", with_save, "Ping", ["Address=example.com"]).raw_payload["Address"] == (
        "example.com"
    )


@pytest.mark.parametrize("commit", [True, False])
def test_cli_submit_of_an_opener_with_an_assignment_is_refused_without_posting(
    tmp_env, clock, monkeypatch, capsys, commit
):
    client, wire = client_with(lambda r, n: html(IPALLOC_HTML))
    monkeypatch.setattr("bgwcli.cli._client_factory", lambda *a, **k: client)
    from bgwcli import cli

    argv = ["submit", "ipalloc", f"Allocate_{MAC}", ALLOC, "--json"]
    code = cli.main([*argv, "--commit", "--confirm", "IPALLOC"] if commit else argv)
    captured = capsys.readouterr()
    assert code == 1
    assert "only opens an editor" in captured.out + captured.err and "set ipalloc" in captured.out + captured.err
    assert [r.method for r in wire.requests] == ["GET"]


def test_cli_submit_of_an_opener_without_assignments_still_opens(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if request.method == "POST":
            return html("", 302, {"location": "/cgi-bin/ipalloc.ha"})
        return html(IPALLOC_HTML)

    code, out, posts = run_cli(
        monkeypatch, capsys, ["submit", "ipalloc", f"Allocate_{MAC}", "--commit", "--confirm", "IPALLOC"], handler
    )
    assert code == 0 and len(posts) == 1 and out["outcome"] == "opened"


def test_cli_submit_of_a_packet_filter_opener_with_an_assignment_is_refused(tmp_env, clock, monkeypatch, capsys):
    from bgwcli import cli

    client, wire = client_with(lambda r, n: html(PACKET_FILTER))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main(["submit", "packetfilter", "AddDropRule", "x=1", "--commit", "--confirm", "PACKETFILTER"])
    captured = capsys.readouterr()
    assert code == 1 and "only opens an editor" in captured.err
    assert [r.method for r in wire.requests] == ["GET"]
