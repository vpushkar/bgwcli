"""A live port forward whose device cannot be resolved (device offline, or two devices sharing the name)
skips the forwards section on the live side of diff, restore and autorestore (2026-11-24 review):
every other section is still compared and restored, the forwards section is never restored or pruned
and does not vote for a reset, and a warning names the forward. `dump` keeps the hard error.
"""

from page_builders import apphosting_page, services_page
from test_autorestore import Fetcher, full_pages, harness, reset_pages
from test_cli import fake, run, run_json, snapshot_fetcher  # noqa: F401 - pytest fixtures

from bgwcli.autorestore import AutorestoreOptions, result_output

OFFLINE = apphosting_page(rows=[("custom_ssh", "offline-host"), ("Mosh", "host-a")])
SHARED = apphosting_page(
    rows=[("custom_ssh", "host-b"), ("Mosh", "host-a")],
    device_options=[("aa:bb:cc:dd:ee:01", "host-b"), ("aa:bb:cc:dd:ee:09", "host-b"), ("aa:bb:cc:dd:ee:02", "host-a")],
)


def _dump(capsys, tmp_path) -> str:
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    return str(out)


def test_diff_skips_an_unresolvable_forward_and_still_compares_the_rest(capsys, fake, snapshot_fetcher, tmp_path):  # noqa: F811
    dump = _dump(capsys, tmp_path)
    snapshot_fetcher["pages"]["apphosting"] = OFFLINE
    code, out, err = run_json(capsys, ["diff", dump, "--json"])
    assert code == 0 and out["identical"] is True
    assert out["forwards"] == {"missing": [], "extra": []}
    assert any("offline-host" in w and "forwards section skipped" in w for w in out["warnings"])
    assert "warning: forwards section skipped" in err

    snapshot_fetcher["pages"]["services"] = services_page(rows=[])
    code, out, _ = run_json(capsys, ["diff", dump, "--json"])
    assert code == 1 and len(out["services"]["missing"]) == 2 and out["forwards"]["missing"] == []


def test_diff_skips_forwards_when_two_devices_share_the_label(capsys, fake, snapshot_fetcher, tmp_path):  # noqa: F811
    dump = _dump(capsys, tmp_path)
    snapshot_fetcher["pages"]["apphosting"] = SHARED
    code, out, _ = run_json(capsys, ["diff", dump, "--json"])
    assert code == 0 and any("2 devices share that name" in w for w in out["warnings"])


def test_restore_plans_the_other_sections_and_never_touches_the_forwards(capsys, fake, snapshot_fetcher, tmp_path):  # noqa: F811
    dump = _dump(capsys, tmp_path)
    rows = [("custom_ssh", "offline-host"), ("Extra", "host-a")]
    snapshot_fetcher["pages"]["apphosting"] = apphosting_page(rows=rows)
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("Mosh", "60001-60010", "60001", "UDP")])
    code, out, _ = run_json(capsys, ["restore", dump, "--prune", "--json"])
    kinds = [(step["kind"], step["page"]) for step in out["steps"]]
    assert ("add-service", "services") in kinds
    assert not any(page == "apphosting" for _, page in kinds), kinds
    assert any("offline-host" in w for w in out["warnings"])


def test_dump_still_refuses_an_unresolvable_forward(capsys, fake, snapshot_fetcher, tmp_path):  # noqa: F811
    snapshot_fetcher["pages"]["apphosting"] = OFFLINE
    code, out, err = run(capsys, ["dump", "--out", str(tmp_path / "dump.json"), "--json"])
    assert code != 0
    assert "offline-host" in out + err and "re-run dump" in out + err
    assert not (tmp_path / "dump.json").exists()


def test_autorestore_without_a_reset_exits_0_with_the_warning():
    live = full_pages()
    live["apphosting"] = OFFLINE
    result, router, _, logs = harness(Fetcher(live), AutorestoreOptions(commit=True))
    assert result.status == "no-reset" and result.exit_code == 0 and router.posts == []
    assert any("offline-host" in w for w in result.warnings)
    assert any(line.startswith("warning: forwards section skipped") for line in logs)
    assert any("offline-host" in w for w in result_output(result)["warnings"])


def test_a_reset_is_still_detected_from_the_other_sections_and_forwards_are_not_restored():
    reset = reset_pages()
    reset["apphosting"] = apphosting_page(rows=[("custom_ssh", "offline-host")])
    result, router, _, _ = harness(Fetcher(reset, full_pages()), AutorestoreOptions(commit=True, max_passes=1))
    assert result.detected is True and "forwards" not in result.reason.split(";")[0]
    assert router.posts and not any(page == "apphosting" for page, _ in router.posts)
