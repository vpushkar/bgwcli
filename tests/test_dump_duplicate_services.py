"""dump refuses a services table the dump loader would refuse to read back."""

from urllib.parse import urlsplit

from integration_html import EMPTY_SECTION_TABLES
from save_helpers import client_with, form, html

from bgwcli import cli

DUPLICATE_SERVICES = (
    '<html><body><form method="post" action="/cgi-bin/services.ha">'
    '<input type="hidden" name="nonce" value="n">'
    "<table><tr><th>Service Name</th><th>Global Port Range</th><th>Base Host Port</th><th>Protocol</th><th></th></tr>"
    '<tr><td>ssh</td><td>22-22</td><td>22</td><td>TCP</td>'
    '<td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
    '<tr><td>SSH </td><td>23-23</td><td>23</td><td>UDP</td>'
    '<td><input type="submit" name="Remove_2" value="Remove"></td></tr>'
    "</table></form></body></html>"
)


def test_dump_with_duplicate_service_names_fails_and_writes_nothing(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if urlsplit(request.url).path.endswith("services.ha"):
            return html(DUPLICATE_SERVICES)
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    path = tmp_env / "backup.json"
    code = cli.main(["dump", "--out", str(path), "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert not path.exists()
    assert "ssh" in captured.out + captured.err
    assert not any(r.method == "POST" and not r.url.endswith("login.ha") for r in wire.requests)


INVALID_FORWARD = (
    '<html><body><form method="post" action="/cgi-bin/apphosting.ha">'
    '<input type="hidden" name="nonce" value="n">'
    '<select name="device"><option value="">Select</option><option value="notamac">Laptop</option></select>'
    "<table><tr><th>Service</th><th>Device</th><th></th></tr>"
    '<tr><td>Web</td><td>Laptop</td><td><input type="submit" name="Remove_1" value="Remove"></td></tr>'
    "</table></form></body></html>"
)


def test_dump_refuses_a_forward_the_loader_would_reject_and_writes_nothing(tmp_env, clock, monkeypatch, capsys):
    def handler(request, n):
        if urlsplit(request.url).path.endswith("apphosting.ha"):
            return html(INVALID_FORWARD)
        return html(form("dosprotect", "old") + EMPTY_SECTION_TABLES)

    client, wire = client_with(handler)
    monkeypatch.setattr(cli, "_client_factory", lambda *a, **k: client)
    path = tmp_env / "backup.json"
    code = cli.main(["dump", "--out", str(path), "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert not path.exists()
    assert "invalid forward entry" in captured.out + captured.err
    assert not any(r.method == "POST" and not r.url.endswith("login.ha") for r in wire.requests)


def test_the_loader_checks_service_names_in_linear_time():
    from bgwcli.dumpfile import _snapshot_problem

    services = [
        {"name": f"s{i}", "protocol": "TCP", "extMinPort": 1, "extMaxPort": 1, "intStartPort": 1}
        for i in range(20000)
    ]
    value = {"meta": {"schema": 2, "firmware": "", "ts": "", "routerHost": ""}, "services": services,
             "forwards": [], "reservations": [], "forms": {}, "tables": {}}
    assert _snapshot_problem(value) is None
    services.append(dict(services[0], name=" S0 "))
    assert "appears more than once" in _snapshot_problem(value)
