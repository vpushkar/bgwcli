"""A type="password" control is secret whatever its name: every display path redacts it, not only
the parser's default (include_secrets=False) view."""

import json
from types import SimpleNamespace

from test_client import FakeTransport, html, make_client

from bgwcli import cli
from bgwcli import format as fmt
from bgwcli.dumpfile import read_dump_file, write_dump_file
from bgwcli.mutations import build_mutation_plan, build_submit_plan
from bgwcli.operations import set_dry_run
from bgwcli.parser import parse_page
from bgwcli.redact import REDACTED
from bgwcli.restore import RestoreOptions, build_restore_plan
from bgwcli.snapshot import extract_snapshot
from bgwcli.snapshot_diff import diff_snapshots

DDNS = (
    '<form method="post" action="/cgi-bin/{page}.ha"><input type="hidden" name="nonce" value="n">'
    '<input type="PASSWORD" name="ddnspin" value="{pin}"><input name="user" value="bob">'
    '<input type="submit" name="Save" value="Save"></form>'
)


def ddns(pin, page="dosprotect"):
    return DDNS.format(page=page, pin=pin)


def wconfig(value, kind="password"):
    return (
        '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="n">'
        f'<input type="{kind}" name="ssidpin11" value="{value}">'
        '<input type="submit" name="Save" value="Save"></form>'
    )


def test_set_dry_run_payload_redacts_password_typed_field():
    parsed = parse_page("dosprotect", ddns("hunter2"), include_secrets=True)
    plan = build_mutation_plan("dosprotect", parsed, ["user=alice"], include_secrets=False)
    assert plan.raw_payload["ddnspin"] == "hunter2"
    result = set_dry_run(plan, "DOSPROTECT")
    assert fmt.operation_output(result)["payload"]["ddnspin"] == REDACTED
    assert "hunter2" not in json.dumps(fmt.operation_output(result))


def test_set_dry_run_changes_redact_password_typed_field():
    parsed = parse_page("dosprotect", ddns("hunter2"), include_secrets=True)
    plan = build_mutation_plan("dosprotect", parsed, ["ddnspin=newpin"], include_secrets=False)
    assert plan.display_changes == {"ddnspin": REDACTED}
    shown = build_mutation_plan("dosprotect", parsed, ["ddnspin=newpin"], include_secrets=True)
    assert shown.display_changes == {"ddnspin": "newpin"}


def test_submit_plan_redacts_password_typed_field():
    parsed = parse_page("dosprotect", ddns("hunter2"), include_secrets=True)
    plan = build_submit_plan("dosprotect", parsed, "Save", ["user=alice"], include_secrets=False)
    assert plan.display_payload["ddnspin"] == REDACTED


def test_cli_set_dry_run_text_never_prints_password_value(tmp_env, capsys, monkeypatch):
    transport = FakeTransport(lambda request, n: html(ddns("hunter2")))
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["set", "dosprotect", "user=alice"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hunter2" not in out
    assert sum(r.method == "POST" for r in transport.requests) == 0


def test_verify_set_mismatch_redacts_password_typed_field(monkeypatch):
    parsed = parse_page("dosprotect", ddns("oldpin"), include_secrets=True)
    plan = build_mutation_plan("dosprotect", parsed, ["ddnspin=newpin"], include_secrets=False)
    transport = FakeTransport(lambda request, n: html(ddns("oldpin")))
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    check = cli._verify_set(client, "dosprotect", plan, include_secrets=False)
    assert check.verified is False
    assert check.mismatches == {"ddnspin": {"wanted": REDACTED, "live": REDACTED}}
    assert sum(r.method == "POST" for r in transport.requests) == 0


def test_verify_set_mismatch_redacts_field_the_reread_renders_as_password():
    plan = SimpleNamespace(raw_payload={"ddnspin": "newpin"}, display_changes={"ddnspin": "x"})
    transport = FakeTransport(lambda request, n: html(ddns("oldpin")))
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    check = cli._verify_set(client, "dosprotect", plan, include_secrets=False)
    assert check.mismatches == {"ddnspin": {"wanted": REDACTED, "live": REDACTED}}


def _diff(dump_kind="password", live_kind="password"):
    dump = extract_snapshot(
        {"wconfig": parse_page("wconfig", wconfig("OldSecret1", dump_kind), include_secrets=True)},
        ts="", router_host="",
    )
    live_pages = {"wconfig": parse_page("wconfig", wconfig("NewSecret2", live_kind), include_secrets=True)}
    live = extract_snapshot(live_pages, ts="", router_host="")
    return dump, live_pages, diff_snapshots(dump, live)


def test_diff_json_and_text_redact_password_typed_field(capsys):
    _, _, diff = _diff()
    shown = json.dumps(fmt.display_diff(diff, False))
    assert "OldSecret1" not in shown and "NewSecret2" not in shown
    fmt.print_snapshot_diff(diff)
    out = capsys.readouterr().out
    assert "OldSecret1" not in out and "NewSecret2" not in out
    assert "ssidpin11" in out
    revealed = json.dumps(fmt.display_diff(diff, True))
    assert "OldSecret1" in revealed and "sensitive" not in revealed


def test_restore_plan_redacts_password_typed_field_everywhere(capsys):
    dump, live_pages, diff = _diff()
    steps = build_restore_plan(diff, dump, live_pages, RestoreOptions(include_secrets=False))
    form = next(s for s in steps if s.kind == "form")
    assert form.raw_payload["ssidpin11"] == "OldSecret1"
    shown = json.dumps(fmt.display_restore_steps(steps, False))
    assert "OldSecret1" not in shown and "NewSecret2" not in shown
    fmt.print_restore_plan(steps)
    out = capsys.readouterr().out
    assert "OldSecret1" not in out and "NewSecret2" not in out
    revealed = json.dumps(fmt.display_restore_steps(
        build_restore_plan(diff, dump, live_pages, RestoreOptions(include_secrets=True)), True
    ))
    assert "OldSecret1" in revealed


def test_dump_records_password_controls_and_diff_uses_them_when_live_does_not(tmp_path, capsys):
    # The dump saw a password control; the live page renders the same name as plain text. The
    # dump's own record keeps the value secret in diff and restore output.
    dump, live_pages, _ = _diff(dump_kind="password", live_kind="text")
    path = tmp_path / "d.json"
    write_dump_file(path, dump)
    assert json.loads(path.read_text())["formSecrets"] == {"wconfig": ["ssidpin11"]}
    loaded = read_dump_file(path)
    live = extract_snapshot(live_pages, ts="", router_host="")
    diff = diff_snapshots(loaded, live)
    assert "OldSecret1" not in json.dumps(fmt.display_diff(diff, False))
    fmt.print_snapshot_diff(diff)
    assert "OldSecret1" not in capsys.readouterr().out
    steps = build_restore_plan(diff, loaded, live_pages, RestoreOptions())
    shown = json.dumps(fmt.display_restore_steps(steps, False))
    assert "OldSecret1" not in shown and "NewSecret2" not in shown
    fmt.print_restore_plan(steps)
    out = capsys.readouterr().out
    assert "OldSecret1" not in out and "NewSecret2" not in out


def test_dump_without_password_controls_keeps_the_old_file_shape(tmp_path):
    dump = extract_snapshot(
        {"wconfig": parse_page("wconfig", wconfig("plain", "text"), include_secrets=True)}, ts="", router_host=""
    )
    path = tmp_path / "d.json"
    write_dump_file(path, dump)
    assert "formSecrets" not in json.loads(path.read_text())


def test_dump_with_malformed_form_secrets_is_refused(tmp_path):
    import pytest

    from bgwcli.errors import DumpFileError

    dump = extract_snapshot(
        {"wconfig": parse_page("wconfig", wconfig("x"), include_secrets=True)}, ts="", router_host=""
    )
    path = tmp_path / "d.json"
    write_dump_file(path, dump)
    data = json.loads(path.read_text())
    data["formSecrets"] = {"wconfig": "ssidpin11"}
    path.write_text(json.dumps(data))
    with pytest.raises(DumpFileError, match="formSecrets"):
        read_dump_file(path)


def test_print_snapshot_diff_reveals_values_with_include_secrets(capsys):
    _, _, diff = _diff()
    fmt.print_snapshot_diff(diff, include_secrets=True)
    out = capsys.readouterr().out
    assert "NewSecret2 -> OldSecret1" in out


def _diff_cli(tmp_env, monkeypatch, capsys, argv):
    dump, _, _ = _diff()
    path = tmp_env / "d.json"
    write_dump_file(path, dump)
    # A readable form (one real control), like every live page: `--include wconfig` never reads it, but a
    # nonce-only page would be refused as control-less if the read set ever grew.
    empty = '<form action="/cgi-bin/x.ha"><input name="nonce" value="n"><input name="note" value=""></form>'
    transport = FakeTransport(lambda request, n: html(wconfig("NewSecret2") if "wconfig" in request.url else empty))
    client = make_client(transport)
    client.import_session({"origin": "http://router.local", "authenticated": True, "cookies": {"sid": "t"}})
    monkeypatch.setattr(cli, "_client_factory", lambda *args, **kwargs: client)
    code = cli.main(["diff", str(path), "--include", "wconfig", *argv])
    assert sum(r.method == "POST" for r in transport.requests) == 0
    return code, capsys.readouterr().out


def test_cli_diff_text_reveals_values_only_with_include_secrets(tmp_env, monkeypatch, capsys):
    code, out = _diff_cli(tmp_env, monkeypatch, capsys, [])
    assert code == 1 and "OldSecret1" not in out and REDACTED in out
    code, out = _diff_cli(tmp_env, monkeypatch, capsys, ["--include-secrets"])
    assert code == 1 and "NewSecret2 -> OldSecret1" in out


def test_restore_plan_keeps_an_unchanged_dumped_password_value_redacted(tmp_path, capsys):
    # ssidpin11 was a password control when dumped and is unchanged; the live page now renders it
    # as text. Only "user" differs, but the save posts the whole form, so the payload carries
    # ssidpin11 and the dump's record must keep it redacted.
    def page(kind, user):
        return (
            '<form action="/cgi-bin/wconfig.ha"><input name="nonce" value="n">'
            f'<input type="{kind}" name="ssidpin11" value="Same1Secret"><input name="user" value="{user}">'
            '<input type="submit" name="Save" value="Save"></form>'
        )

    dump = extract_snapshot(
        {"wconfig": parse_page("wconfig", page("password", "alice"), include_secrets=True)}, ts="", router_host=""
    )
    path = tmp_path / "d.json"
    write_dump_file(path, dump)
    loaded = read_dump_file(path)
    live_pages = {"wconfig": parse_page("wconfig", page("text", "bob"), include_secrets=True)}
    diff = diff_snapshots(loaded, extract_snapshot(live_pages, ts="", router_host=""))
    steps = build_restore_plan(diff, loaded, live_pages, RestoreOptions())
    form = next(s for s in steps if s.kind == "form")
    assert form.raw_payload["ssidpin11"] == "Same1Secret"
    assert "ssidpin11" in form.sensitive_names
    shown = json.dumps(fmt.display_restore_steps(steps, False))
    assert "Same1Secret" not in shown and "alice" in shown
    fmt.print_restore_plan(steps)
    assert "Same1Secret" not in capsys.readouterr().out
    revealed = build_restore_plan(diff, loaded, live_pages, RestoreOptions(include_secrets=True))
    assert "Same1Secret" in json.dumps(fmt.display_restore_steps(revealed, True))
