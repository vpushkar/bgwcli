"""Sweep engine tests. Parser/pages/fetch/status/devices are injected as fakes through the
module-level seams in bgwcli.sweep so these tests run before those modules exist."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from sweep_helpers import (
    FAKE_TABS,
    FakeClient,
    FakeDeviceListResult,
)

from bgwcli import sweep
from bgwcli.errors import RouterAuthError, UsageError
from bgwcli.sweep import (
    SweepOptions,
    SweepPage,
    strip_large_payloads,
    sweep_exit_code,
    sweep_router,
    write_sweep_artifacts,
)
from bgwcli.types import (
    ParsedPage,
    to_json_dict,
)


def test_sweep_walks_selected_pages_in_router_tab_order(backend):
    client = FakeClient()
    pages = sweep_router(
        client, SweepOptions(delay_ms=0, pages=["diag", "wconfig_unified", "dhcpserver"], use_fallbacks=False)
    )
    assert [page.page for page in pages] == ["wconfig_unified", "dhcpserver", "diag"]
    assert client.calls == ["wconfig_unified", "dhcpserver", "diag"]


def test_sweep_resolves_aliases_and_dedupes_tabs(backend):
    client = FakeClient()
    pages = sweep_router(client, SweepOptions(pages=[" troubleshoot ", "wifi", "diag"], use_fallbacks=False))
    assert [page.page for page in pages] == ["wconfig_unified", "diag"]

    everything = sweep_router(FakeClient(), SweepOptions(use_fallbacks=False))
    assert [page.page for page in everything].count("sitemap") == 1
    assert len(everything) == len({tab.page for tab in FAKE_TABS})


def test_sweep_rejects_unknown_pages(backend):
    with pytest.raises(UsageError, match="Unknown sweep page\\(s\\): nope"):
        sweep_router(FakeClient(), SweepOptions(pages=["diag", "nope"], use_fallbacks=False))


def test_sweep_continues_after_per_page_failure(backend):
    client = FakeClient(fail_pages={"dhcpserver"})
    pages = sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert [(page.page, page.ok) for page in pages] == [("dhcpserver", False), ("diag", True)]
    assert "boom" in (pages[0].error or "")
    assert pages[0].data_count == 0 and pages[0].data_obtainable is False and pages[0].useful is False


def test_sweep_records_a_page_level_403_and_continues(backend):
    """One page answering 401/403 is that page's failure, not a lost session: the sweep goes on
    (as it did before GET status codes were checked), while login failures still abort below."""
    client = FakeClient(forbidden_pages={"dhcpserver"})
    pages = sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert [(page.page, page.ok) for page in pages] == [("dhcpserver", False), ("diag", True)]
    assert "HTTP 403" in (pages[0].error or "")
    assert client.calls == ["dhcpserver", "diag"]


def test_sweep_aborts_on_a_login_403_even_though_it_carries_a_status(backend):
    client = FakeClient(login_forbidden_pages={"dhcpserver"})
    with pytest.raises(RouterAuthError):
        sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert client.calls == ["dhcpserver"]


def test_sweep_aborts_immediately_on_authentication_errors(backend):
    client = FakeClient(auth_error_pages={"dhcpserver"})
    with pytest.raises(RouterAuthError):
        sweep_router(client, SweepOptions(pages=["dhcpserver", "diag"], use_fallbacks=False))
    assert client.calls == ["dhcpserver"]


def test_sweep_marks_remaining_pages_skipped_when_session_pool_is_full(backend):
    client = FakeClient(session_pool_full_pages={"dhcpserver"})
    progress = []
    pages = sweep_router(
        client,
        SweepOptions(
            pages=["wconfig_unified", "dhcpserver", "diag"], use_fallbacks=False, on_page_progress=progress.append
        ),
    )
    assert client.calls == ["wconfig_unified", "dhcpserver"]
    assert [(p.page, p.ok, p.skipped, p.session_pool_full) for p in pages] == [
        ("wconfig_unified", True, None, None),
        ("dhcpserver", False, None, True),
        ("diag", False, True, True),
    ]
    assert pages[1].waited_ms == 5 and pages[1].retry_count == 2
    assert pages[2].error == "Skipped because router web session pool is full."
    events = [to_json_dict(event) for event in progress]
    assert {"index": 3, "total": 3, "page": "diag", "phase": "finish", "status": "skipped"} in events
    assert events[0] == {"index": 1, "total": 3, "page": "wconfig_unified", "phase": "start"}
    assert events[1] == {"index": 1, "total": 3, "page": "wconfig_unified", "phase": "finish", "status": "ok"}
    assert events[3] == {"index": 2, "total": 3, "page": "dhcpserver", "phase": "finish", "status": "failed"}


def test_sweep_default_result_is_compact_and_detail_is_opt_in(backend):
    compact = sweep_router(FakeClient(), SweepOptions(pages=["diag"], use_fallbacks=False))
    page = compact[0]
    assert page.page == "diag" and page.ok is True
    assert (page.value_count, page.value_entry_count, page.table_rows) == (2, 1, 0)
    assert (page.field_count, page.button_count, page.form_count, page.link_count) == (2, 1, 1, 0)
    assert (page.select_count, page.textarea_count) == (0, 0)
    assert page.data_count == 6
    assert (page.data_obtainable, page.useful, page.not_only_junk) == (True, True, True)
    assert (page.dangerous, page.guarded, page.status_code) == (False, False, 200)
    assert page.parsed is None and page.raw_html is None and page.controls is None
    as_json = to_json_dict(page)
    assert "parsed" not in as_json and "rawHtml" not in as_json and "controls" not in as_json
    assert as_json["valueCount"] == 2 and as_json["notOnlyJunk"] is True and as_json["statusCode"] == 200

    detailed = sweep_router(
        FakeClient(),
        SweepOptions(pages=["diag"], include_parsed=True, include_forms=True, include_raw=True, use_fallbacks=False),
    )
    assert detailed[0].parsed is not None and detailed[0].parsed.page == "diag"
    assert "<title>diag</title>" in (detailed[0].raw_html or "")
    assert [button.name for button in detailed[0].controls.buttons] == ["Ping"]
    assert to_json_dict(detailed[0])["controls"]["buttons"][0]["name"] == "Ping"


def test_sweep_flags_junk_pages_as_not_ok(backend):
    pages = sweep_router(FakeClient(login_pages={"diag"}), SweepOptions(pages=["diag"], use_fallbacks=False))
    assert pages[0].ok is False and pages[0].title == "Login"
    assert pages[0].data_obtainable is True and pages[0].useful is False and pages[0].not_only_junk is False

    pages = sweep_router(FakeClient(status_codes={"diag": 500}), SweepOptions(pages=["diag"], use_fallbacks=False))
    assert pages[0].ok is False and pages[0].status_code == 500


def test_sweep_fetches_sitemap_without_auth(backend):
    client = FakeClient()
    sweep_router(client, SweepOptions(pages=["sitemap", "diag"], use_fallbacks=False))
    assert client.kwargs["sitemap"] == {"auth": False}
    assert client.kwargs["diag"] == {}


def test_sweep_marks_dangerous_tabs_guarded(backend):
    pages = sweep_router(FakeClient(), SweepOptions(pages=["restart"], use_fallbacks=False))
    assert pages[0].dangerous is True and pages[0].guarded is True
    assert (pages[0].section, pages[0].label) == ("Device", "Restart Device")


def test_sweep_sleeps_between_pages_when_delay_set(backend):
    sweep_router(FakeClient(), SweepOptions(delay_ms=250, pages=["diag", "dhcpserver"], use_fallbacks=False))
    assert backend == [250], "no sleep after the last page"
    backend.clear()
    sweep_router(FakeClient(), SweepOptions(delay_ms=0, pages=["diag"], use_fallbacks=False))
    assert backend == []


def test_sweep_exposes_fallback_section_data_only_with_parsed_detail(backend):
    compact = sweep_router(FakeClient(fail_pages={"home"}), SweepOptions(pages=["home"]))
    assert (compact[0].page, compact[0].ok, compact[0].fallback) == ("home", True, True)
    assert compact[0].title == "Device Status fallback"
    assert compact[0].fallback_sections is None
    assert compact[0].value_count == 3 and compact[0].table_rows == 3 and compact[0].data_count == 6
    assert "boom" in (compact[0].error or "")

    detailed = sweep_router(FakeClient(fail_pages={"home"}), SweepOptions(pages=["home"], include_parsed=True))
    assert [section.page for section in detailed[0].fallback_sections] == ["sysinfo", "broadbandstatistics", "firewall"]
    assert all(section.values for section in detailed[0].fallback_sections)
    assert to_json_dict(detailed[0])["fallbackSections"][0]["page"] == "sysinfo"


def test_sweep_uses_status_views_when_pages_load_directly(backend):
    client = FakeClient()
    pages = sweep_router(client, SweepOptions(pages=["home", "lanstatistics", "securityoptions"]))
    assert [(p.page, p.ok, p.fallback) for p in pages] == [
        ("home", True, None),
        ("lanstatistics", True, None),
        ("securityoptions", True, None),
    ]
    assert pages[0].title == "home" and pages[0].data_count == 6

    lan_fallback = sweep_router(FakeClient(fail_pages={"lanstatistics"}), SweepOptions(pages=["lanstatistics"]))
    assert lan_fallback[0].title == "Home Network Status fallback" and lan_fallback[0].fallback is True
    sec_fallback = sweep_router(FakeClient(fail_pages={"securityoptions"}), SweepOptions(pages=["securityoptions"]))
    assert sec_fallback[0].title == "Security Options fallback" and sec_fallback[0].fallback is True


def test_sweep_devices_fallback_counts_devices(backend):
    compact = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))
    page = compact[0]
    assert (page.ok, page.fallback, page.title) == (True, False, "Device List")
    assert page.table_rows == 2 and page.data_count == 2 and page.devices is None
    detailed = sweep_router(FakeClient(), SweepOptions(pages=["devices"], include_parsed=True))
    assert len(detailed[0].devices) == 2


def test_sweep_devices_page_with_no_online_devices_is_ok(backend, monkeypatch):
    """A healthy router with zero online devices answered: the page is ok and the sweep exits 0."""
    monkeypatch.setattr(sweep, "_fetch_device_list", lambda client: FakeDeviceListResult(False, []))
    pages = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))
    assert (pages[0].ok, pages[0].error, pages[0].data_count) == (True, None, 0)
    assert sweep_exit_code(pages) == 0


def test_sweep_devices_page_is_not_ok_when_the_device_list_could_not_be_read(backend, monkeypatch):
    monkeypatch.setattr(sweep, "_fetch_device_list", lambda client: FakeDeviceListResult(True, [], error="boom"))
    pages = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))
    assert (pages[0].ok, pages[0].fallback, pages[0].error) == (False, True, "boom")
    assert sweep_exit_code(pages) == 2


def test_sweep_devices_page_keeps_the_fallback_error_when_ip_allocation_was_unreadable(backend, monkeypatch):
    monkeypatch.setattr(
        sweep,
        "_fetch_device_list",
        lambda client: FakeDeviceListResult(True, [], error="timed out", fallback_error="ipalloc answered 403"),
    )
    page = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))[0]
    assert (page.ok, page.error, page.fallback_error) == (False, "timed out", "ipalloc answered 403")
    assert to_json_dict(page)["fallbackError"] == "ipalloc answered 403"
    monkeypatch.setattr(sweep, "_fetch_device_list", lambda client: FakeDeviceListResult(False, [object()]))
    healthy = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))[0]
    assert healthy.fallback_error is None and "fallbackError" not in to_json_dict(healthy)


def test_sweep_devices_page_rebuilt_from_ip_allocation_is_ok(backend, monkeypatch):
    """devices.ha failed but the IP Allocation fallback produced devices: the page answered."""
    monkeypatch.setattr(
        sweep, "_fetch_device_list", lambda client: FakeDeviceListResult(True, [object()], error="timed out")
    )
    page = sweep_router(FakeClient(), SweepOptions(pages=["devices"]))[0]
    assert (page.ok, page.fallback, page.error, page.data_count) == (True, True, "timed out", 1)


def test_sweep_generic_fallback_path_reports_unusable_pages(backend):
    login = sweep_router(FakeClient(login_pages={"diag"}), SweepOptions(pages=["diag"]))
    assert login[0].ok is False
    assert login[0].error == "Router returned the login page instead of the requested page."
    assert login[0].data_count == 6  # parsed page still counted, like TS

    broken = sweep_router(FakeClient(fail_pages={"diag"}), SweepOptions(pages=["diag"]))
    assert broken[0].ok is False and "boom" in broken[0].error and broken[0].data_count == 0


def test_sweep_raw_disables_fallbacks(backend):
    client = FakeClient(fail_pages={"home"})
    pages = sweep_router(client, SweepOptions(pages=["home"], include_raw=True))
    assert pages[0].ok is False and pages[0].fallback is None
    assert client.calls == ["home"]


def test_strip_large_payloads_drops_raw_parsed_and_controls(backend):
    detailed = sweep_router(
        FakeClient(),
        SweepOptions(pages=["diag"], include_parsed=True, include_forms=True, include_raw=True, use_fallbacks=False),
    )[0]
    compact = strip_large_payloads(detailed)
    assert isinstance(compact, SweepPage)
    assert compact.parsed is None and compact.raw_html is None and compact.controls is None
    assert compact.value_count == detailed.value_count and compact.page == "diag"
    assert detailed.parsed is not None  # original untouched


def test_write_sweep_artifacts_writes_html_parsed_and_compact_sweep_json(backend, tmp_path):
    pages = sweep_router(
        FakeClient(fail_pages={"dhcpserver"}),
        SweepOptions(pages=["diag", "dhcpserver"], include_parsed=True, include_raw=True, use_fallbacks=False),
    )
    out = tmp_path / "out"
    written = write_sweep_artifacts(pages, out)

    html_path = out / "router-html" / "diag.html"
    parsed_path = out / "parsed" / "diag.json"
    assert html_path.read_text().endswith("</form>\n")
    assert "<title>diag</title>" in html_path.read_text()
    parsed_json = json.loads(parsed_path.read_text())
    assert parsed_json["page"] == "diag" and parsed_json["valueEntries"][0]["label"] == "Status"
    assert not (out / "router-html" / "dhcpserver.html").exists()
    assert not (out / "parsed" / "dhcpserver.json").exists()

    by_page = {page.page: page for page in written}
    assert by_page["diag"].artifacts.html == str(html_path) and by_page["diag"].artifacts.parsed == str(parsed_path)
    assert by_page["diag"].raw_html is None and by_page["diag"].parsed is None
    assert by_page["dhcpserver"].artifacts is None

    sweep_json = json.loads((out / "sweep.json").read_text())
    assert [entry["page"] for entry in sweep_json] == ["dhcpserver", "diag"]  # router-tab order
    entries = {entry["page"]: entry for entry in sweep_json}
    assert entries["diag"]["artifacts"] == {"html": str(html_path), "parsed": str(parsed_path)}
    assert "rawHtml" not in entries["diag"] and "parsed" not in entries["diag"]
    assert "artifacts" not in entries["dhcpserver"]


def _artifact_page(raw_html: str = "<html><title>diag</title></html>") -> SweepPage:
    return SweepPage(
        section="Diagnostics",
        label="Diagnostics",
        page="diag",
        dangerous=False,
        guarded=False,
        ok=True,
        raw_html=raw_html,
        parsed=ParsedPage(page="diag", title="diag", heading="diag"),
    )


def _artifact_paths(out: Path) -> list[Path]:
    return [out / "router-html" / "diag.html", out / "parsed" / "diag.json", out / "sweep.json"]


@pytest.mark.parametrize("umask", [0o022, 0o000])
def test_write_sweep_artifacts_files_are_private_under_any_umask(tmp_path, umask):
    out = tmp_path / "out"
    previous = os.umask(umask)
    try:
        write_sweep_artifacts([_artifact_page()], out)
    finally:
        os.umask(previous)
    for path in _artifact_paths(out):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


@pytest.mark.parametrize("umask", [0o022, 0o000])
def test_write_sweep_artifacts_directories_are_private_under_any_umask(tmp_path, umask):
    out = tmp_path / "out"
    previous = os.umask(umask)
    try:
        write_sweep_artifacts([_artifact_page()], out)
    finally:
        os.umask(previous)
    for directory in (out, out / "router-html", out / "parsed"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_write_sweep_artifacts_tightens_existing_world_readable_files(tmp_path):
    out = tmp_path / "out"
    for path in _artifact_paths(out):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale contents that are much longer than the new artifact " * 50)
        path.chmod(0o644)
    write_sweep_artifacts([_artifact_page()], out)
    for path in _artifact_paths(out):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert "stale" not in (out / "router-html" / "diag.html").read_text(encoding="utf-8")


def test_write_sweep_artifacts_are_utf8_regardless_of_locale(tmp_path):
    """A non-UTF-8 locale must not change the artifact encoding (or crash on characters it cannot encode)."""
    out = tmp_path / "out"
    script = (
        "import sys\n"
        "from bgwcli.sweep import SweepPage, write_sweep_artifacts\n"
        "from bgwcli.types import ParsedPage\n"
        "page = SweepPage(section='D', label='D', page='diag', dangerous=False, guarded=False, ok=True,\n"
        "                 raw_html='<p>caf\\u00e9 \\u2713</p>',\n"
        "                 parsed=ParsedPage(page='diag', title='caf\\u00e9 \\u2713', heading=''))\n"
        "write_sweep_artifacts([page], sys.argv[1])\n"
    )
    src = Path(__file__).resolve().parent.parent / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONUTF8": "0", "LC_ALL": "en_US.ISO8859-1"}
    env.pop("PYTHONIOENCODING", None)
    probe = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    if probe.stdout.strip().lower().replace("-", "").replace("_", "") != "iso88591":
        pytest.skip(f"ISO-8859-1 locale unavailable (subprocess encoding {probe.stdout.strip()!r})")
    subprocess.run([sys.executable, "-c", script, str(out)], check=True, env=env, capture_output=True)

    assert (out / "router-html" / "diag.html").read_bytes() == "<p>caf\u00e9 \u2713</p>\n".encode()
    assert json.loads((out / "parsed" / "diag.json").read_bytes().decode("utf-8"))["title"] == "caf\u00e9 \u2713"
    (out / "sweep.json").read_bytes().decode("utf-8")


def test_sweep_page_json_keys_are_camel_case(backend):
    client = FakeClient(session_pool_full_pages={"diag"})
    page = sweep_router(client, SweepOptions(pages=["diag"], use_fallbacks=False))[0]
    as_json = to_json_dict(page)
    assert set(as_json) >= {
        "section", "label", "page", "dangerous", "guarded", "ok", "error", "valueCount", "tableRows",
        "fieldCount", "selectCount", "textareaCount", "buttonCount", "formCount", "dataCount",
        "dataObtainable", "useful", "notOnlyJunk", "sessionPoolFull", "waitedMs", "retryCount",
    }
    assert "skipped" not in as_json and "statusCode" not in as_json


def test_sweep_exit_code_is_2_when_every_swept_page_failed(backend):
    pages = sweep_router(
        FakeClient(fail_pages={"diag", "dhcpserver"}), SweepOptions(pages=["diag", "dhcpserver"], use_fallbacks=False)
    )
    assert [page.ok for page in pages] == [False, False]
    assert sweep_exit_code(pages) == 2


def test_sweep_exit_code_is_0_when_any_page_succeeded(backend):
    pages = sweep_router(
        FakeClient(fail_pages={"dhcpserver"}), SweepOptions(pages=["diag", "dhcpserver"], use_fallbacks=False)
    )
    assert [page.ok for page in pages] == [False, True]
    assert sweep_exit_code(pages) == 0
    all_ok = sweep_router(FakeClient(), SweepOptions(pages=["diag"], use_fallbacks=False))
    assert [page.ok for page in all_ok] == [True] and sweep_exit_code(all_ok) == 0


def test_sweep_exit_code_is_0_for_an_empty_sweep():
    assert sweep_exit_code([]) == 0


@pytest.mark.parametrize("relative", ["router-html/diag.html", "parsed/diag.json", "sweep.json"])
def test_write_sweep_artifacts_refuses_a_symlinked_artifact_and_leaves_its_target_alone(tmp_path, relative):
    out = tmp_path / "out"
    (out / "router-html").mkdir(parents=True)
    (out / "parsed").mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("KEEP THIS DATA")
    unrelated.chmod(0o644)
    (out / relative).symlink_to(unrelated)
    with pytest.raises(PermissionError, match="symlink"):
        write_sweep_artifacts([_artifact_page()], out)
    assert unrelated.read_text() == "KEEP THIS DATA"
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert (out / relative).is_symlink()


@pytest.mark.parametrize("directory", ["router-html", "parsed"])
def test_write_sweep_artifacts_refuses_a_symlinked_artifact_directory(tmp_path, directory):
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    (out / directory).symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        write_sweep_artifacts([_artifact_page()], out)
    assert list(elsewhere.iterdir()) == []
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755


def test_fixture_capture_refuses_a_symlinked_fixture_file(tmp_path):
    import io

    from bgwcli.audit import capture_fixture_pack

    root = tmp_path / "fixtures"
    (root / "router-html").mkdir(parents=True)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("KEEP THIS DATA")
    (root / "router-html" / "diag.html").symlink_to(unrelated)
    with pytest.raises(PermissionError, match="symlink"):
        capture_fixture_pack([_artifact_page()], root, stdout=io.StringIO())
    assert unrelated.read_text() == "KEEP THIS DATA"


def test_write_sweep_artifacts_refuses_an_artifact_owned_by_another_user(tmp_path, monkeypatch):
    out = tmp_path / "out"
    write_sweep_artifacts([_artifact_page()], out)
    before = (out / "sweep.json").read_text()
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(PermissionError, match="another user"):
        write_sweep_artifacts([_artifact_page("<p>new</p>")], out)
    assert (out / "sweep.json").read_text() == before


def _tree(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_a_symlinked_output_root_is_refused_before_anything_is_written(tmp_path):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        write_sweep_artifacts([_artifact_page()], alias)
    assert _tree(physical) == []


def test_an_output_root_owned_by_another_user_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(out).st_uid + 1)
    with pytest.raises(PermissionError, match="another user"):
        write_sweep_artifacts([_artifact_page()], out)
    assert _tree(out) == []


def test_a_refused_child_leaves_no_other_artifact_behind(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (out / "parsed").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        write_sweep_artifacts([_artifact_page()], out)
    assert _tree(out) == ["parsed"] and _tree(elsewhere) == []


def test_a_refused_missing_root_is_not_created(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "fixtures").write_text("a file, not a directory")
    with pytest.raises(NotADirectoryError):
        write_sweep_artifacts([_artifact_page()], parent / "fixtures")
    assert _tree(parent) == ["fixtures"]


def test_fixture_capture_refuses_a_symlinked_root_before_writing_any_page(tmp_path):
    import io

    from bgwcli.audit import capture_fixture_pack

    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        capture_fixture_pack([_artifact_page()], alias, stdout=io.StringIO())
    assert _tree(physical) == []


def test_sweep_preflight_refuses_a_symlinked_per_page_file_before_any_walk(backend, tmp_path):
    from bgwcli.sweep import preflight_sweep_output

    html_dir = tmp_path / "router-html"
    html_dir.mkdir()
    (html_dir / "diag.html").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PermissionError, match="symlink"):
        preflight_sweep_output(tmp_path)
    with pytest.raises(PermissionError, match="symlink"):
        preflight_sweep_output(tmp_path, ["diag"])
    preflight_sweep_output(tmp_path, ["home"])  # a page that is not walked is not checked


def test_fixture_preflight_refuses_a_symlinked_per_page_file_before_any_walk(backend, tmp_path):
    from bgwcli.audit import preflight_fixture_root

    expected = tmp_path / "expected"
    expected.mkdir()
    (expected / "diag.json").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PermissionError, match="symlink"):
        preflight_fixture_root(tmp_path, ["diag"])
    preflight_fixture_root(tmp_path, ["home"])
