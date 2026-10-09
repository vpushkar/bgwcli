"""Selected recovery reads and extraction stay isolated from unrelated broken sections."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from page_builders import (
    apphosting_page,
    dosprotect_page,
    ipalloc_page,
    page,
    select,
    services_page,
    sysinfo_page,
)

from bgwcli import cli
from bgwcli.autorestore import AutorestoreOptions, run_autorestore
from bgwcli.dumpfile import write_dump_file
from bgwcli.restore import RestoreOptions, build_restore_plan, restore_converged
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots
from bgwcli.types import HttpResponse


def firewall(value):
    return dosprotect_page(selects=[select("flood_protect", ["on", "off"], selected=value)])


def baseline_pages():
    return {
        "sysinfo": sysinfo_page(),
        "services": services_page(),
        "apphosting": apphosting_page(),
        "ipalloc": ipalloc_page(),
        "packetfilter": page("packetfilter"),
        "dosprotect": firewall("on"),
        "wconfig": page("wconfig"),
        "wconfig_unified": page("wconfig_unified"),
    }


def snapshot(pages):
    return extract_snapshot(pages, ts="synthetic", router_host="synthetic.invalid")


class ScopedRouter:
    """No ownership read is allowed when restoring a selected form or forwarding section."""

    def __init__(self, pages):
        self.pages = pages
        self.posts = []

    def get_cgi_page(self, page_id, **kwargs):
        raise AssertionError(f"unexpected direct GET {page_id}")

    def post_cgi_page(self, page_id, fields):
        self.posts.append((page_id, dict(fields)))
        if page_id == "dosprotect":
            self.pages[page_id] = firewall(fields["flood_protect"])
        elif page_id == "apphosting":
            self.pages[page_id] = apphosting_page()
        else:
            raise AssertionError(f"unexpected write to {page_id}")
        from integration_html import saved_configuration_html
        return HttpResponse(
            200, "OK", {}, saved_configuration_html(page_id, fields),
            f"https://synthetic.invalid/cgi-bin/{page_id}.ha",
        )


class Collector:
    """Deliberately returns extra parsed pages: callers must project before extraction too."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, client, page_ids):
        self.calls.append(tuple(page_ids))
        return self.pages, []


@pytest.mark.parametrize("command_name,commit", [("diff", False), ("restore", False), ("restore", True)])
def test_firewall_scope_ignores_unresolved_forward_and_unselected_allocations(
    tmp_path, tmp_env, monkeypatch, capsys, command_name, commit
):
    saved = snapshot(baseline_pages())
    dump_path = tmp_path / "baseline.json"
    write_dump_file(dump_path, saved)
    live = baseline_pages()
    live["dosprotect"] = firewall("off")
    live["apphosting"] = apphosting_page(rows=[("custom_ssh", "offline-host")], device_options=[])
    live["ipalloc"] = page("ipalloc", tables=[{"unrecognized": "allocation"}])
    router = ScopedRouter(live)
    collector = Collector(live)
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", collector)
    args = [command_name, str(dump_path), "--include", "dosprotect", "--json"]
    if commit:
        args += ["--commit", "--confirm", "RESTORE"]
    command = cli.parse_args(args)

    getattr(cli, f"_run_{command_name}")(router, command)

    output = json.loads(capsys.readouterr().out)
    assert collector.calls == [("dosprotect",)] * (2 if commit else 1)
    if command_name == "diff":
        assert command.exit_code == 1 and output["forms"]["dosprotect"]
        assert output["firmwareChanged"] is False
    elif commit:
        assert command.exit_code == 0 and output["diff"]["identical"] is True
        assert output["diff"]["firmwareChanged"] is False
        assert router.posts == [("dosprotect", {"flood_protect": "on", "Save": "Save"})]
    else:
        assert [(step["page"], step["kind"]) for step in output["steps"]] == [("dosprotect", "form")]
    if not commit:
        assert router.posts == []


def test_autorestore_scope_projects_both_detection_and_closing_snapshot():
    saved = snapshot(baseline_pages())
    live = baseline_pages()
    live["dosprotect"] = firewall("off")
    live["apphosting"] = apphosting_page(rows=[("custom_ssh", "offline-host")], device_options=[])
    live["ipalloc"] = page("ipalloc", tables=[{"unrecognized": "allocation"}])
    router = ScopedRouter(live)
    collector = Collector(live)

    result = run_autorestore(
        lambda: (router, False), saved, AutorestoreOptions(commit=True, pages=("dosprotect",)),
        fetch_pages=collector, log=lambda _: None,
    )

    assert result.status == "converged" and result.exit_code == 0
    assert collector.calls == [("dosprotect",), ("dosprotect",)]
    assert result.final_diff is not None and result.final_diff.firmware_changed is False
    assert [page_id for page_id, _ in router.posts] == ["dosprotect"]


@pytest.mark.parametrize("command_name", ["diff", "restore"])
def test_forward_scope_keeps_mac_lookup_and_service_dropdowns_without_other_sections(
    tmp_path, tmp_env, monkeypatch, capsys, command_name
):
    saved = snapshot(baseline_pages())
    dump_path = tmp_path / "baseline.json"
    write_dump_file(dump_path, saved)
    live = baseline_pages()
    # One present forward exercises device-label -> MAC extraction; one missing needs the Add controls.
    live["apphosting"] = apphosting_page(rows=[("custom_ssh", "host-b")])
    live["services"] = page("services", tables=[{"unrecognized": "service"}])
    live["ipalloc"] = page("ipalloc", tables=[{"unrecognized": "allocation"}])
    collector = Collector(live)
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", collector)
    command = cli.parse_args([command_name, str(dump_path), "--include", "apphosting", "--json"])

    getattr(cli, f"_run_{command_name}")(ScopedRouter(live), command)

    output = json.loads(capsys.readouterr().out)
    assert collector.calls == [("apphosting",)]
    if command_name == "diff":
        assert output["forwards"]["missing"] == [
            {"service": "Mosh", "deviceLabel": "host-a", "deviceMac": "aa:bb:cc:dd:ee:02"}
        ]
        assert output["forwards"]["extra"] == []
    else:
        assert len(output["steps"]) == 1
        assert output["steps"][0]["kind"] == "add-forward"
        assert output["steps"][0].get("blocked") is None
        assert output["steps"][0]["displayPayload"]["device"] == "aa:bb:cc:dd:ee:02"


@pytest.mark.parametrize("command_name", ["diff", "restore"])
def test_missing_requested_optional_page_is_nothing_compared_without_fetching_anything(
    tmp_path, tmp_env, monkeypatch, capsys, command_name
):
    dump_path = tmp_path / "baseline.json"
    write_dump_file(dump_path, snapshot(baseline_pages()))
    collector = Collector({})
    monkeypatch.setattr(cli, "_fetch_snapshot_pages", collector)
    command = cli.parse_args([command_name, str(dump_path), "--include", "dhcpserver", "--json"])

    getattr(cli, f"_run_{command_name}")(ScopedRouter({}), command)

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert command.exit_code == 1 and output["missingPages"] == ["dhcpserver"]
    assert output["ok"] is False and "nothing compared: dhcpserver not in dump" in output["error"]
    assert collector.calls == []


@pytest.mark.parametrize("prune", [False, True])
def test_live_only_form_fields_do_not_prevent_convergence(prune):
    saved = snapshot(baseline_pages())
    live = replace(saved, forms={**saved.forms, "dosprotect": {"flood_protect": "on", "new_option": "on"}})
    diff = diff_snapshots(saved, live, pages=("dosprotect",))

    assert not build_restore_plan(diff, saved, baseline_pages(), RestoreOptions(pages=("dosprotect",), prune=prune))
    assert diff.forms["dosprotect"][0].dump is None  # The read-only diff remains informative.
    assert restore_converged(diff, prune)


@pytest.mark.parametrize("prune", [False, True])
@pytest.mark.parametrize("wanted_value", [None, "off"])
def test_missing_or_different_saved_form_value_still_prevents_convergence(prune, wanted_value):
    saved = snapshot(baseline_pages())
    form = {"new_option": "on"}
    if wanted_value is not None:
        form["flood_protect"] = wanted_value
    live = replace(saved, forms={**saved.forms, "dosprotect": form})

    assert not restore_converged(diff_snapshots(saved, live, pages=("dosprotect",)), prune)


def test_absent_live_firmware_remains_unknown_without_reporting_a_change():
    saved = snapshot(baseline_pages())
    live = snapshot({"dosprotect": firewall("on")})

    assert live.meta.firmware == ""
    assert diff_snapshots(saved, live, pages=("dosprotect",)).firmware_changed is False
