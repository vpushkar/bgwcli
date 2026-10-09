"""A fixture-capture run never overwrites a real captured fixture with a failure placeholder: the page
that failed keeps its previous fixture and is listed as degraded."""

from __future__ import annotations

import io
import json

from sweep_helpers import FakeClient

from bgwcli import cli
from bgwcli.audit import capture_fixture_pack
from bgwcli.sweep import SweepOptions, sweep_router

PLACEHOLDER = "<!-- bgw fixture capture failed for"


def _capture(tmp_path, client, pages, *, degraded=None):
    swept = sweep_router(
        client, SweepOptions(pages=pages, include_raw=True, include_parsed=True, use_fallbacks=False)
    )
    out = io.StringIO()
    kwargs = {} if degraded is None else {"degraded": degraded}
    captured = capture_fixture_pack(swept, tmp_path, stdout=out, **kwargs)
    return captured, out.getvalue()


def _snapshot(tmp_path, page):
    return {
        directory: (tmp_path / directory / f"{page}.{ext}").read_bytes()
        for directory, ext in (("router-html", "html"), ("parsed", "json"), ("expected", "json"))
    }


def test_a_page_that_fails_keeps_its_previous_fixture_and_is_listed_degraded(backend, tmp_path):
    first, _ = _capture(tmp_path, FakeClient(), ["diag", "dhcpserver"])
    assert first == 2
    before = _snapshot(tmp_path, "dhcpserver")
    assert PLACEHOLDER not in before["router-html"].decode()

    degraded: list[str] = []
    failing = FakeClient(fail_pages={"dhcpserver"})
    captured, text = _capture(tmp_path, failing, ["diag", "dhcpserver"], degraded=degraded)

    assert captured == 1
    assert _snapshot(tmp_path, "dhcpserver") == before
    assert degraded == ["dhcpserver"]
    assert "degraded" in text and "dhcpserver" in text.splitlines()[-1]


def test_a_failed_page_without_a_previous_fixture_still_gets_its_placeholder(backend, tmp_path):
    degraded: list[str] = []
    captured, _ = _capture(tmp_path, FakeClient(fail_pages={"dhcpserver"}), ["dhcpserver"], degraded=degraded)
    assert captured == 0
    assert (tmp_path / "router-html" / "dhcpserver.html").read_text().startswith(PLACEHOLDER)
    assert degraded == []


def test_a_previous_placeholder_is_replaced_by_a_real_capture(backend, tmp_path):
    _capture(tmp_path, FakeClient(fail_pages={"diag"}), ["diag"])
    assert (tmp_path / "router-html" / "diag.html").read_text().startswith(PLACEHOLDER)
    captured, _ = _capture(tmp_path, FakeClient(), ["diag"])
    assert captured == 1
    assert not (tmp_path / "router-html" / "diag.html").read_text().startswith(PLACEHOLDER)


def test_fixtures_capture_json_receipt_names_the_degraded_pages(backend, tmp_path, monkeypatch, capsys, tmp_env):
    from test_cli import PAGES
    from test_cli import FakeClient as CliClient

    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    monkeypatch.setenv("BGW_ACCESS_CODE", "unused")
    root = tmp_path / "fx"
    client = CliClient()
    assert cli.main(["fixtures-capture", "--out", str(root), "--pages", "diag,devices", "--json"]) == 0
    capsys.readouterr()
    kept = (root / "router-html" / "devices.html").read_bytes()

    client = CliClient({page: html for page, html in PAGES.items() if page != "devices"})
    code = cli.main(["fixtures-capture", "--out", str(root), "--pages", "diag,devices", "--json"])
    receipt = json.loads(capsys.readouterr().out)
    assert code == 0, "partial success keeps exit 0"
    assert receipt["captured"] == 1 and receipt["total"] == 2
    assert receipt["degraded"] == ["devices"]
    assert [p["degraded"] for p in receipt["pages"] if p["page"] == "devices"] == [True]
    assert (root / "router-html" / "devices.html").read_bytes() == kept
