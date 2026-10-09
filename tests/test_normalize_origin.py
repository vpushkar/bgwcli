"""Malformed hosts are usage errors, never a silently different origin."""

from __future__ import annotations

import pytest

from bgwcli.client import normalize_origin
from bgwcli.errors import UsageError


@pytest.mark.parametrize("host", [
    "192.168.1.254", "https://192.168.1.254/", "HTTP://Router.Local:80", "router.local:8443", "[fe80::1]", "http://[::1]:8080",
])
def test_well_formed_hosts_keep_their_origin(host):
    assert normalize_origin(host).startswith(("http://", "https://"))


def test_origin_forms():
    assert normalize_origin("HTTP://Router.Local:80/") == "http://router.local"
    assert normalize_origin("router.local:8443") == "https://router.local:8443"
    assert normalize_origin("http://[::1]:8080") == "http://[::1]:8080"


@pytest.mark.parametrize("host", [
    "", "https://", "http://:80", "router.local:notaport", "router.local:99999", "router.local:0", "[fe80::1", "http://[nope]",
    "bad host", "user:secret@router.local", "https://user@router.local", "router.local/cgi-bin/home.ha",
    "router.local?x=1", "router.local#frag", "rou\nter", "ftp://router.local",
])
def test_malformed_hosts_are_usage_errors(host):
    with pytest.raises(UsageError):
        normalize_origin(host)


@pytest.mark.parametrize("command", [["page", "services"], ["session", "status"], ["check"]])
def test_a_malformed_host_flag_or_variable_is_exit_1_before_any_router_contact(tmp_env, monkeypatch, capsys, command):
    from bgwcli import cli

    def no_network(*args, **kwargs):
        raise AssertionError("the router must not be contacted")

    monkeypatch.setattr("bgwcli.client.urllib_transport", no_network)
    assert cli.main([*command, "--host", "router.local:notaport", "--json"]) == 1
    monkeypatch.setenv("BGW_HOST", "user:pw@router.local")
    assert cli.main([*command, "--json"]) == 1
    assert "Invalid router host" in capsys.readouterr().err
