import json
import os
import stat
from dataclasses import replace

import pytest
from test_sweep import FakeClient, backend, fake_parse_page, fake_parsed, fake_parsed_data_count  # noqa: F401

from bgwcli.audit import (
    ExpectedFixture,
    build_audit,
    capture_fixture_pack,
    expected_fixture,
    write_fixture_set,
)
from bgwcli.errors import BgwError
from bgwcli.sweep import SweepOptions, SweepPage, sweep_router
from bgwcli.types import ParsedField, ParsedPage, to_json_dict


def scan(**overrides) -> SweepPage:
    data_count = overrides.get("data_count", 0)
    ok = overrides.get("ok", False)
    base = SweepPage(
        section="Device",
        label="Status",
        page="home",
        dangerous=False,
        guarded=False,
        ok=False,
        data_count=data_count,
        data_obtainable=data_count > 0,
        useful=ok and data_count > 0,
        not_only_junk=ok and data_count > 0,
    )
    return replace(base, **overrides)


def test_build_audit_classifies_failed_fallback_useful_and_empty_pages():
    scans = [
        scan(section="Device", label="Status", page="home", ok=True, fallback=True, data_count=12),
        scan(section="Diagnostics", label="Update", page="update", dangerous=True, guarded=True, ok=True, data_count=0),
        scan(section="Voice", label="Call Statistics", page="voicestat", ok=False, error="Timed out"),
    ]
    audit = build_audit(scans)
    assert (audit.total_pages, audit.ok_pages, audit.failed_pages) == (3, 2, 1)
    assert (audit.fallback_pages, audit.useful_pages, audit.empty_pages, audit.dangerous_pages) == (1, 1, 1, 1)
    assert [page.useful for page in audit.pages] == [True, False, False]
    assert audit.pages[2].error == "Timed out"


def test_build_audit_recomputes_useful_from_ok_and_data_count():
    # A page claiming useful=True without data is corrected, as in TS buildAudit.
    audit = build_audit([scan(ok=True, data_count=0, useful=True)])
    assert audit.pages[0].useful is False and audit.useful_pages == 0 and audit.empty_pages == 1


def test_audit_json_keys_match_ts():
    audit = build_audit([scan(ok=True, data_count=3)])
    as_json = to_json_dict(audit)
    assert set(as_json) == {
        "totalPages", "okPages", "failedPages", "fallbackPages", "usefulPages", "emptyPages", "dangerousPages", "pages",
    }
    assert as_json["pages"][0]["dataCount"] == 3 and as_json["pages"][0]["useful"] is True
    assert build_audit([]).total_pages == 0


def test_expected_fixture_summarises_parsed_page(monkeypatch):
    parsed = fake_parsed("diag")
    parsed.tables = [{"Test": "Ethernet", "Result": "Pass"}, {"Test": "DNS"}]
    expected = expected_fixture("diag", parsed, page_loads=True, data_count=fake_parsed_data_count(parsed))

    assert isinstance(expected, ExpectedFixture)
    assert (expected.page, expected.title, expected.page_loads) == ("diag", "diag", True)
    assert expected.data_obtainable is True and expected.useful_fields_exist is True
    assert expected.useful_tables_exist is True and expected.buttons_discovered is True
    assert expected.forms_discovered is True and expected.secrets_redacted is True and expected.not_only_junk is True
    assert to_json_dict(expected.counts) == {
        "values": 2, "valueEntries": 1, "tables": 2, "fields": 2, "selects": 0,
        "textareas": 0, "buttons": 1, "forms": 1, "links": 0,
    }
    assert expected.value_keys == ["Status", "Title"]
    assert expected.table_columns == ["Result", "Test"]
    assert expected.field_names == ["nonce", "target"]
    assert expected.button_names == ["Ping"] and expected.form_actions == ["/cgi-bin/diag.ha"]
    assert expected.select_names == [] and expected.textarea_names == [] and expected.link_targets == []
    assert expected.value_entry_labels == ["Status"]

    as_json = to_json_dict(expected)
    assert set(as_json) == {
        "page", "title", "pageLoads", "dataObtainable", "usefulFieldsExist", "usefulTablesExist",
        "buttonsDiscovered", "formsDiscovered", "secretsRedacted", "notOnlyJunk", "counts", "valueKeys",
        "tableColumns", "fieldNames", "selectNames", "textareaNames", "buttonNames", "formActions",
        "linkTargets", "valueEntryLabels",
    }


def test_expected_fixture_flags_junk_and_unredacted_pages():
    empty = ParsedPage(page="x", title="Page not found", heading="Page not found.")
    expected = expected_fixture("x", empty, page_loads=False, data_count=0)
    assert expected.data_obtainable is False and expected.not_only_junk is False
    assert expected.useful_fields_exist is False and expected.useful_tables_exist is False
    assert expected.buttons_discovered is False and expected.forms_discovered is False

    only_nonce = ParsedPage(
        page="y", title="y", heading="y", fields=[ParsedField("nonce", "hidden", "[redacted]", False, True)]
    )
    assert expected_fixture("y", only_nonce, page_loads=True, data_count=1).useful_fields_exist is False

    login = ParsedPage(page="z", title="Login", heading="", values={"a": "b"})
    assert expected_fixture("z", login, page_loads=True, data_count=1).not_only_junk is False

    leaked = ParsedPage(
        page="w", title="w", heading="w", fields=[ParsedField("password", "password", "hunter2", False, True)]
    )
    assert expected_fixture("w", leaked, page_loads=True, data_count=1).secrets_redacted is False


def test_write_fixture_set_writes_owner_only_files_in_fixture_layout(tmp_path):
    parsed = fake_parsed("diag")
    expected = expected_fixture("diag", parsed, page_loads=True, data_count=6)
    paths = write_fixture_set(tmp_path, "diag", "<title>diag</title>\n\n", parsed, expected, error="boom")

    assert [p.relative_to(tmp_path).as_posix() for p in paths] == [
        "router-html/diag.html", "parsed/diag.json", "expected/diag.json",
    ]
    assert paths[0].read_text() == "<title>diag</title>\n"
    assert json.loads(paths[1].read_text())["page"] == "diag"
    expected_json = json.loads(paths[2].read_text())
    assert expected_json["pageLoads"] is True and expected_json["error"] == "boom"
    for path in paths:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    without_error = write_fixture_set(tmp_path, "diag", "<title>diag</title>", parsed, expected)
    assert "error" not in json.loads(without_error[2].read_text())


def test_capture_fixture_pack_sanitizes_and_writes_every_swept_page(backend, tmp_path, capsys):  # noqa: F811
    pages = sweep_router(
        FakeClient(fail_pages={"dhcpserver"}),
        SweepOptions(pages=["diag", "dhcpserver"], include_raw=True, include_parsed=True, use_fallbacks=False),
    )
    captured = capture_fixture_pack(pages, tmp_path)
    assert captured == 1

    html = (tmp_path / "router-html" / "diag.html").read_text()
    assert 'name="nonce" value="[redacted]"' in html
    expected = json.loads((tmp_path / "expected" / "diag.json").read_text())
    assert expected["pageLoads"] is True and expected["secretsRedacted"] is True

    failed_html = (tmp_path / "router-html" / "dhcpserver.html").read_text()
    assert failed_html.startswith("<!-- bgw fixture capture failed for dhcpserver: boom dhcpserver -->")
    failed_expected = json.loads((tmp_path / "expected" / "dhcpserver.json").read_text())
    assert failed_expected["pageLoads"] is False and failed_expected["error"] == "boom dhcpserver"
    assert (tmp_path / "parsed" / "dhcpserver.json").exists()

    out = capsys.readouterr().out
    assert "capturing diag... ok" in out and "capturing dhcpserver... failed: boom dhcpserver" in out


def test_capture_fixture_pack_refuses_sensitive_residue(backend, tmp_path, monkeypatch):  # noqa: F811
    from bgwcli import audit as audit_module

    def leaky_parse(page, html, include_secrets=False):
        parsed = fake_parse_page(page, html, include_secrets)
        parsed.fields = [ParsedField("password", "password", "hunter2", False, True)]
        return parsed

    monkeypatch.setattr(audit_module, "_parse_page", leaky_parse)
    pages = sweep_router(
        FakeClient(), SweepOptions(pages=["diag"], include_raw=True, include_parsed=True, use_fallbacks=False)
    )
    with pytest.raises(BgwError, match="Refusing to write diag"):
        capture_fixture_pack(pages, tmp_path)
    assert not (tmp_path / "router-html" / "diag.html").exists()
