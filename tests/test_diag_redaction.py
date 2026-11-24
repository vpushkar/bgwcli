"""Diagnostics result pages: a "Pass" result is not a secret.

diag.ha and its details pages lay out results as <th scope="row">Test</th><td>Pass</td>[<td>button</td>].
The parser used to take the first such row for a header row, so its "Pass" named a column and every
later result under it was redacted (live fw 6.35.8, 2026-11-24). A row whose only header cell is its
label is a labelled data row, not column names.
"""

from bgwcli.parser import parse_page

DIAG = (
    "<html><head><title>Diagnostics</title></head><body><table>"
    '<tr><th scope="row">Ethernet</th><td class="respass">Pass</td>'
    '<td><input type="submit" name="EthDetails" value="Details"></td></tr>'
    '<tr><th scope="row">Authentication</th><td class="respass">Pass</td>'
    '<td><input type="submit" name="AuthDetails" value="Details"></td></tr>'
    '<tr><th scope="row">IP</th><td class="respass">Pass</td>'
    '<td><input type="submit" name="IPDetails" value="Details"></td></tr>'
    "</table></body></html>"
)
ETHERDIAG = (
    "<html><head><title>Ethernet Diagnostics</title></head><body><table>"
    '<tr><th scope="row">Ethernet Ports</th><td><span class="respass">Pass</span></td></tr>'
    '<tr><th scope="row">Forwarding / Bridging</th><td><span class="respass">Pass</span></td></tr>'
    "</table></body></html>"
)


def _cells(parsed):
    return [value for row in parsed.tables for value in row.values()]


def test_every_diag_result_reads_pass():
    parsed = parse_page("diag", DIAG)
    assert "[redacted]" not in _cells(parsed), parsed.tables
    assert _cells(parsed).count("Pass") == 3


def test_every_details_result_reads_pass():
    parsed = parse_page("etherdiag", ETHERDIAG)
    assert parsed.values.get("Forwarding / Bridging") == "Pass", parsed.values
    assert parsed.values.get("Ethernet Ports") == "Pass"


def test_a_secret_row_label_still_redacts_its_values():
    page = (
        "<html><body><table>"
        '<tr><th scope="row">Network</th><td>home</td><td>x</td></tr>'
        '<tr><th scope="row">Password</th><td>hunter2</td><td>x</td></tr>'
        "</table></body></html>"
    )
    assert "hunter2" not in str(parse_page("p", page).tables)


def test_a_real_header_row_still_redacts_its_secret_column():
    page = (
        "<html><body><table>"
        "<tr><th>SSID</th><th>Password</th><th>Band</th></tr>"
        "<tr><td>home</td><td>hunter2</td><td>5</td></tr>"
        "</table></body></html>"
    )
    assert "hunter2" not in str(parse_page("p", page).tables)
