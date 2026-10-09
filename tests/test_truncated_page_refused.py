"""A page the parser cut at one of its bounds is a structural read failure for every write plan and
snapshot read: no POST, nothing written, and no removal is planned from the rows that were read."""

import json
from urllib.parse import urlsplit

import pytest
from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, form, html

from bgwcli import cli
from bgwcli import parser as parser_module
from bgwcli.dumpfile import write_dump_file
from bgwcli.errors import SnapshotExtractionError
from bgwcli.parser import parse_page
from bgwcli.snapshot import Snapshot, SnapshotMeta, extract_snapshot

# The element bound is lowered for this module so a small filler trips it; the real 150000-element
# bound is exercised in test_parser_bounds.py and test_truncated_consumers.py.
TEST_MAX_ELEMENTS = 1000
FILLER = "<span></span>" * (TEST_MAX_ELEMENTS + 100)


@pytest.fixture(autouse=True)
def small_element_bound(monkeypatch):
    monkeypatch.setattr(parser_module, "MAX_ELEMENTS", TEST_MAX_ELEMENTS)


BIG_FORM = (
    '<html><body><form action="/cgi-bin/dosprotect.ha"><input name="nonce" value="a">'
    f'<input name="a" value="1">{FILLER}<input name="late" value="2">'
    '<input type="submit" name="Save" value="Save"></form></body></html>'
)
BIG_SERVICES = (
    '<html><body><form method="post" action="/cgi-bin/services.ha">'
    '<input type="hidden" name="nonce" value="n">'
    "<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>"
    '<tr><td>extra</td><td>22-22</td><td>22</td><td>TCP</td>'
    '<td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
    f"{FILLER}</table></form></body></html>"
)


def _run(monkeypatch, capsys, handler, argv):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    out = json.loads(capsys.readouterr().out)
    posts = [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]
    return code, out, posts


def test_the_filler_page_is_truncated():
    assert parse_page("dosprotect", BIG_FORM, include_secrets=True).truncated is True


@pytest.mark.parametrize("argv", [
    ["set", "dosprotect", "a=2", "--commit", "--confirm", "DOSPROTECT"],
    ["submit", "dosprotect", "Save", "a=2", "--commit", "--confirm", "DOSPROTECT"],
])
def test_set_and_submit_refuse_a_truncated_form_before_any_post(tmp_env, clock, monkeypatch, capsys, argv):
    code, out, posts = _run(monkeypatch, capsys, lambda r, n: html(BIG_FORM), argv)
    assert posts == [] and code == 2
    assert out["ok"] is False and "partly read" in out["error"]


BIG_PACKETFILTER = (
    '<html><body><form action="/cgi-bin/packetfilter.ha"><input name="nonce" value="n">'
    "<table><tr><th>Rule</th><th>Action</th></tr><tr><td>allow all</td><td>Drop</td></tr>"
    f"{FILLER}</table><input type=\"submit\" name=\"AddDropRule\" value=\"Add a 'Drop' Rule\"></form></body></html>"
)


def test_dump_keeps_going_without_a_truncated_documentary_page(tmp_env, clock, monkeypatch, capsys):
    # packetfilter is documentary (recorded as text, never restored) and best effort by contract: a page
    # that cannot be read, a parser-cut one included, is a stderr warning and the dump continues without it.
    def handler(request, n):
        if urlsplit(request.url).path.endswith("packetfilter.ha"):
            return html(BIG_PACKETFILTER)
        return html(form("wconfig", "old") + EMPTY_SECTION_TABLES)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    path = tmp_env / "backup.json"
    code = cli.main(["dump", "--out", str(path)])
    captured = capsys.readouterr()
    assert code == 0, captured.out + captured.err
    assert "warning: documentary page 'packetfilter' could not be read" in captured.err
    assert "partly read" in captured.err
    written = json.loads(path.read_text())
    assert "packetfilter" not in written["tables"]
    assert not [r for r in wire.requests if r.method == "POST" and not r.url.endswith("login.ha")]


def test_dump_writes_nothing_for_a_truncated_page(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if urlsplit(request.url).path.endswith("dosprotect.ha"):
            return html(BIG_FORM)
        return html(form("wconfig", "old") + EMPTY_SECTION_TABLES)

    path = tmp_env / "backup.json"
    code, out, posts = _run(monkeypatch, capsys, handler, ["dump", "--out", str(path)])
    assert code == 2 and posts == [] and not path.exists()
    assert [f["page"] for f in out["failures"]] == ["dosprotect"]


def test_diff_exits_2_for_a_truncated_page(tmp_env, clock, monkeypatch, capsys):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local"), forms={"dosprotect": {"a": "1"}}))
    code, out, posts = _run(monkeypatch, capsys, lambda r, n: html(BIG_FORM), ["diff", str(path)])
    assert code == 2 and posts == [] and "partly read" in json.dumps(out)


def test_prune_plans_no_removal_from_a_truncated_services_read(tmp_env, clock, monkeypatch, capsys):
    path = tmp_env / "dump.json"
    write_dump_file(path, Snapshot(SnapshotMeta("", "", "router.local")))
    code, out, posts = _run(
        monkeypatch, capsys,
        lambda r, n: html(BIG_SERVICES if urlsplit(r.url).path.endswith("services.ha") else EMPTY_SECTION_TABLES),
        ["restore", str(path), "--include", "services", "--prune", "--commit", "--confirm", "RESTORE"],
    )
    assert posts == [] and code == 2
    assert "Remove_1" not in json.dumps(out) and "partly read" in json.dumps(out)


def test_extract_snapshot_refuses_a_truncated_page():
    parsed = parse_page("dosprotect", BIG_FORM, include_secrets=True)
    with pytest.raises(SnapshotExtractionError):
        extract_snapshot({"dosprotect": parsed}, ts="", router_host="")


@pytest.mark.parametrize("argv", [["page", "dosprotect"], ["firewall", "dosprotect"]], ids=["page", "section command"])
def test_page_still_shows_a_truncated_page_and_says_so(tmp_env, clock, monkeypatch, capsys, argv):
    client, _wire = client_with(lambda r, n: html(BIG_FORM))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["truncated"] is True
    assert "partly read" in captured.err
