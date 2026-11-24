"""Printed commit commands keep the operator's target and scope.

Review finding (2026-11-24): `--host 192.0.2.80 action factory-reset` previewed
`action factory-reset --commit --confirm FACTORY-RESET`, which run on its own resolves to the default
gateway; scoped restore previews dropped `--include`.
"""

from test_cli import apphosting_page, fake, run, run_json, services_page, snapshot_fetcher  # noqa: F401

from bgwcli.operations import quote_arg


def _action_preview(capsys, *extra):
    code, payload, _ = run_json(capsys, ["action", "factory-reset", "--json", *extra])
    assert code == 0, payload
    return payload["commitCommand"]


def test_an_explicit_host_is_kept_in_the_action_commit_command(capsys, fake):  # noqa: F811
    command = _action_preview(capsys, "--host", "192.0.2.80")
    assert command == "action factory-reset --commit --confirm FACTORY-RESET --host 192.0.2.80"


def test_the_default_host_adds_nothing(capsys, fake):  # noqa: F811
    assert _action_preview(capsys) == "action factory-reset --commit --confirm FACTORY-RESET"


def test_a_host_from_the_environment_is_written_out(capsys, fake, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BGW_HOST", "192.0.2.81")
    assert _action_preview(capsys).endswith(" --host 192.0.2.81")


def test_an_explicit_config_file_is_kept(capsys, fake, tmp_path):  # noqa: F811
    config = tmp_path / "my bgw.conf"
    config.write_text("BGW_HOST='192.0.2.82'\n")
    command = _action_preview(capsys, "--config", str(config))
    assert f"--config '{config}'" in command
    assert "--host 192.0.2.82" in command


def test_set_and_diagnostics_previews_keep_the_host(capsys, fake):  # noqa: F811
    argv = ["diagnostics", "ping", "example.com", "--ipv6", "--host", "192.0.2.80", "--json"]
    code, payload, _ = run_json(capsys, argv)
    assert code == 0, payload
    assert payload["commitCommand"] == "diagnostics ping example.com --commit --ipv6 --host 192.0.2.80"


def test_a_scoped_restore_preview_keeps_dump_include_prune_and_host(capsys, fake, snapshot_fetcher, tmp_path):  # noqa: F811
    out = tmp_path / "dump.json"
    assert run(capsys, ["dump", "--out", str(out)])[0] == 0
    snapshot_fetcher["pages"]["services"] = services_page(rows=[("custom_ssh", "2483-2483", "22", "TCP")])
    snapshot_fetcher["pages"]["apphosting"] = apphosting_page(rows=[("custom_ssh", "host-b")])
    code, payload, _ = run_json(
        capsys, ["restore", str(out), "--include", "services", "--prune", "--host", "192.0.2.80", "--json"]
    )
    assert code == 0, payload
    assert payload["operation"]["commitCommand"] == (
        f"bgwcli restore {quote_arg(str(out))} --include services --prune "
        "--commit --confirm RESTORE --host 192.0.2.80"
    )
