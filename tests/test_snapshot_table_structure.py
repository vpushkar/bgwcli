"""A services, forwards or reservations page without its recognizable table is a failed read, never
an empty section: an empty section in a dump lets `restore --prune` delete what the dump was meant to
preserve, and an empty live section makes diff/restore plan adds for rows that are really there."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from integration_html import APPHOSTING_HTML, IPALLOC_HTML, SERVICES_HTML, gateway_empty_page, header_only
from save_helpers import client_with, form, html

from bgwcli import cli
from bgwcli.parser import parse_page
from bgwcli.snapshot import missing_table_structure

PLEASE_WAIT = "<title>Temporarily unavailable</title><h1>Please wait</h1>"


@pytest.mark.parametrize("page,body", [
    ("services", SERVICES_HTML), ("apphosting", APPHOSTING_HTML), ("ipalloc", IPALLOC_HTML),
])
def test_header_only_table_is_a_legitimate_empty_section(page, body):
    empty = header_only(body)
    assert missing_table_structure(page, parse_page(page, empty), empty) is None
    # Parsed rows carrying the section's columns prove the table without the raw body.
    assert missing_table_structure(page, parse_page(page, body), None) is None


@pytest.mark.parametrize("page", ["services", "apphosting", "ipalloc"])
def test_page_without_the_table_is_reported(page):
    reason = missing_table_structure(page, parse_page(page, PLEASE_WAIT), PLEASE_WAIT)
    assert reason is not None and "table" in reason and "not read as an empty section" in reason
    assert missing_table_structure(page, None, None) is not None


@pytest.mark.parametrize("page", ["apphosting", "services"])
def test_the_gateways_empty_marker_is_a_legitimate_empty_section(page):
    # Live fw 6.35.8 renders no header row on an empty page: one spanning cell with the
    # "No ... entries have been defined" sentence. Nothing else on the page names the columns.
    body = gateway_empty_page(page)
    assert missing_table_structure(page, parse_page(page, body), body) is None


def test_another_sections_empty_marker_is_not_this_table():
    body = gateway_empty_page("apphosting", marker_page="services")
    assert missing_table_structure("apphosting", parse_page("apphosting", body), body) is not None


def test_the_entry_forms_service_name_label_is_not_the_services_table():
    # The live services page labels its entry form's first row "Service Name" in a row header. That
    # label must not pass for the table header: a page showing neither the header row nor the empty
    # marker is a failed read (here the marker is another section's sentence).
    body = gateway_empty_page("services", marker_page="apphosting").replace(
        '<input id="name" type="text" name="Service" value="">',
        '<tr><th scope="row"><label for="name">Service Name</label></th>'
        '<td><input id="name" type="text" name="Service" value=""></td></tr>',
    )
    assert "Service Name" in body
    assert missing_table_structure("services", parse_page("services", body), body) is not None


@pytest.mark.parametrize("page", ["apphosting", "services"])
def test_a_marker_table_that_also_carries_rows_is_not_the_empty_table(page):
    # The marker is the whole table: a table whose first row is the sentence but which carries data rows
    # below is not the gateway's empty rendering, and reading it as empty would hide those rows.
    body = gateway_empty_page(page).replace("</th></tr></table>", "</th></tr><tr><td>Mosh</td></tr></table>")
    assert "<td>Mosh</td>" in body
    assert missing_table_structure(page, parse_page(page, body), body) is not None


def test_the_ip_allocation_table_has_no_empty_marker():
    # ipalloc was never observed empty and has no marker: a one-cell table with another section's
    # sentence, and no IP Allocation header, stays a failed read.
    body = gateway_empty_page("apphosting").replace("apphosting.ha", "ipalloc.ha")
    assert missing_table_structure("ipalloc", parse_page("ipalloc", body), body) is not None


def test_the_empty_marker_needs_the_raw_body():
    # The marker is a one-cell row, never a parsed data row: without the body nothing proves the table.
    body = gateway_empty_page("apphosting")
    assert missing_table_structure("apphosting", parse_page("apphosting", body), None) is not None


def test_renamed_header_is_not_the_table():
    renamed = header_only(SERVICES_HTML).replace("Service Name", "Rule Label")
    assert missing_table_structure("services", parse_page("services", renamed), renamed) is not None


def test_form_pages_need_no_table():
    body = form("dosprotect", "old")
    assert missing_table_structure("dosprotect", parse_page("dosprotect", body), body) is None


def _invoke(monkeypatch, capsys, args, handler):
    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **kw: client)
    code = cli.main([*args, "--host", "http://router.local", "--json"])
    return code, json.loads(capsys.readouterr().out), wire


def _page_of(request) -> str:
    return urlsplit(request.url).path.rsplit("/", 1)[-1].removesuffix(".ha")


def test_dump_refuses_a_services_page_without_its_table(tmp_env, clock, monkeypatch, capsys):
    path = Path(tmp_env) / "dump.json"

    def handler(request, _n):
        page = _page_of(request)
        if page == "services":
            return html(PLEASE_WAIT)
        if page == "apphosting":
            return html(header_only(APPHOSTING_HTML))
        if page == "ipalloc":
            return html(header_only(IPALLOC_HTML))
        return html(form(page, "old"))

    code, out, wire = _invoke(monkeypatch, capsys, ["dump", "--out", str(path)], handler)
    assert code == 2 and out["ok"] is False
    assert [f["page"] for f in out["failures"]] == ["services"]
    assert not path.exists()
    assert sum(r.method == "POST" for r in wire.requests) == 0


def _empty_gateway(request, _n):
    """A gateway with every service and forward removed (the post-factory-reset shape)."""
    page = _page_of(request)
    if page in ("services", "apphosting"):
        return html(gateway_empty_page(page))
    if page == "ipalloc":
        return html(header_only(IPALLOC_HTML))
    return html(form(page, "old"))


def test_dump_reads_the_gateways_empty_pages_as_empty_sections(tmp_env, clock, monkeypatch, capsys):
    path = Path(tmp_env) / "dump.json"
    code, out, _wire = _invoke(monkeypatch, capsys, ["dump", "--out", str(path)], _empty_gateway)
    assert code == 0, out
    written = json.loads(path.read_text())
    assert written["services"] == [] and written["forwards"] == []


def test_diff_reports_every_dumped_row_missing_from_the_gateways_empty_pages(tmp_env, clock, monkeypatch, capsys):
    dump = Path(tmp_env) / "dump.json"
    dump.write_text(json.dumps({
        "meta": {"schema": 2, "firmware": "", "ts": "", "routerHost": "router.local"},
        "services": [{"name": "SSH", "extMinPort": 2222, "extMaxPort": 2222, "intStartPort": 22, "protocol": "TCP"}],
        "forwards": [{"service": "SSH", "deviceLabel": "host-b", "deviceMac": "aa:bb:cc:dd:ee:01"}],
        "reservations": [], "forms": {}, "tables": {},
    }))
    code, out, wire = _invoke(monkeypatch, capsys, ["diff", str(dump)], _empty_gateway)
    assert code == 1, out
    assert out["identical"] is False
    assert [s["name"] for s in out["services"]["missing"]] == ["SSH"]
    assert [f["service"] for f in out["forwards"]["missing"]] == ["SSH"]
    assert sum(r.method == "POST" for r in wire.requests) == 0


def test_restore_prune_never_reads_a_missing_live_table_as_empty(tmp_env, clock, monkeypatch, capsys):
    dump = Path(tmp_env) / "dump.json"
    dump.write_text(json.dumps({
        "meta": {"schema": 2, "firmware": "", "ts": "", "routerHost": "router.local"},
        "services": [{"name": "SSH", "extMinPort": 2222, "extMaxPort": 2222, "intStartPort": 22, "protocol": "TCP"}],
        "forwards": [], "reservations": [], "forms": {}, "tables": {},
    }))

    def handler(request, _n):
        page = _page_of(request)
        if page == "services":
            return html(PLEASE_WAIT + '<form action="/cgi-bin/services.ha"><input name="nonce" value="n"></form>')
        if page == "apphosting":
            return html(header_only(APPHOSTING_HTML))
        return html(form(page, "old"))

    code, out, wire = _invoke(
        monkeypatch, capsys,
        ["restore", str(dump), "--include", "services", "--prune", "--commit", "--confirm", "RESTORE"], handler,
    )
    posts = [parse_qs(r.body.decode()) for r in wire.requests if r.method == "POST"]
    assert code == 2 and out["ok"] is False
    assert [f["page"] for f in out["failures"]] == ["services"]
    assert posts == []
