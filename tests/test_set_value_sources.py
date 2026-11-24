"""`set` / `submit` values from stdin or a file, so a Wi-Fi key never sits in argv.

KEY=@-      the value is read from stdin (one per invocation; trailing newline dropped)
KEY=@<path> the value is read from that file (trailing newline dropped; a warning when the file is
            readable by group or others)
KEY=@@text  the literal value "@text"
"""

import io
import json

import pytest
from save_helpers import client_with, html

from bgwcli import cli
from bgwcli.errors import UsageError
from bgwcli.mutations import resolve_assignment_values

SECRET = "N3wW1f1Key!"
WCONFIG = (
    '<html><head><title>Wi-Fi</title></head><body><form method="post" action="/cgi-bin/wconfig.ha">'
    '<input type="hidden" name="nonce" value="abc123">'
    '<input type="text" name="ssidname11" value="home">'
    '<input type="text" name="key11" value="OldKey123">'
    '<input type="submit" name="Save" value="Save"></form></body></html>'
)


def test_stdin_value_replaces_the_marker():
    out, used = resolve_assignment_values(["key11=@-", "ssidname11=home"], stdin=io.StringIO(SECRET + "\n"))
    assert out == [f"key11={SECRET}", "ssidname11=home"] and used is True


def test_only_one_stdin_value_per_invocation():
    with pytest.raises(UsageError):
        resolve_assignment_values(["key11=@-", "key12=@-"], stdin=io.StringIO("a\nb\n"))


def test_file_value_and_permission_warning(tmp_path):
    secret_file = tmp_path / "key"
    secret_file.write_text(SECRET + "\n")
    secret_file.chmod(0o644)
    warnings = []
    out, used = resolve_assignment_values([f"key11=@{secret_file}"], warn=warnings.append)
    assert out == [f"key11={SECRET}"] and used is False
    assert warnings and str(secret_file) in warnings[0] and SECRET not in warnings[0]
    secret_file.chmod(0o600)
    warnings.clear()
    resolve_assignment_values([f"key11=@{secret_file}"], warn=warnings.append)
    assert warnings == []


def test_missing_file_names_the_path_only(tmp_path):
    with pytest.raises(UsageError) as caught:
        resolve_assignment_values([f"key11=@{tmp_path / 'nope'}"])
    assert "nope" in str(caught.value)


def test_double_at_is_a_literal():
    out, _ = resolve_assignment_values(["ssidname11=@@home"])
    assert out == ["ssidname11=@home"]


def test_access_code_stdin_and_stdin_value_are_refused(monkeypatch, capsys, tmp_env):
    monkeypatch.setattr("sys.stdin", io.StringIO("code\n"))
    code = cli.main(["set", "wconfig", "key11=@-", "--access-code-stdin", "--json"])
    out = capsys.readouterr()
    assert code == 1 and "stdin" in json.loads(out.out)["error"]


def test_set_dry_run_reads_the_key_from_stdin_and_never_prints_it(monkeypatch, capsys, tmp_env, clock):
    client, wire = client_with(lambda request, n: html(WCONFIG))
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    code = cli.main(["set", "wconfig", "key11=@-", "--json"])
    out = capsys.readouterr()
    assert code == 0, out
    assert SECRET not in out.out and SECRET not in out.err
    data = json.loads(out.out)
    assert "key11" in data["changes"]
    assert not any(r.method == "POST" for r in wire.requests)
