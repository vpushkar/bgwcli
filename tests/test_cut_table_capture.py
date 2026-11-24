"""A page that ends inside a data table (a response cut mid-row) is never taken as the whole section.

A 200 answer without Content-Length that ended in the second services row used to yield a one-service
section: the dump passed, and a later `--prune` against the full page planned Remove_2 for the service
the dump never saw.
"""

import pytest
from integration_html import APPHOSTING_HTML, IPALLOC_HTML, SERVICES_HTML

from bgwcli.parser import parse_page
from bgwcli.snapshot import SnapshotExtractionError, extract_snapshot


def _cut(html: str, marker: str) -> str:
    index = html.index(marker)
    return html[: index + len(marker)]


def _snapshot(page: str, html: str):
    return extract_snapshot(
        {page: parse_page(page, html, include_secrets=True)},
        ts="2026-11-24T00:00:00Z", router_host="router.local", selected_pages=(page,),
    )


def test_the_whole_services_page_still_reads_both_services():
    assert [s.name for s in _snapshot("services", SERVICES_HTML).services] == ["custom_ssh", "Mosh"]


@pytest.mark.parametrize(
    "marker",
    ["<tr><td>Mosh</td><td>60001-60010</td>", "<tr><td>Mosh</td>", "</tr>\n<tr>"],
    ids=["mid-row", "row-start", "between-rows"],
)
def test_a_services_page_cut_inside_its_table_fails_the_capture(marker):
    with pytest.raises(SnapshotExtractionError, match="ended inside"):
        _snapshot("services", _cut(SERVICES_HTML, marker))


@pytest.mark.parametrize(("page", "html"), [("apphosting", APPHOSTING_HTML), ("ipalloc", IPALLOC_HTML)])
def test_other_table_sections_cut_inside_their_table_fail_too(page, html):
    cut = html[: html.index("</table>") - 5]
    with pytest.raises(SnapshotExtractionError, match="ended inside"):
        _snapshot(page, cut)


def test_a_page_cut_after_its_table_closed_is_still_read():
    # The table is complete; only the trailing add-form controls are missing.
    cut = _cut(SERVICES_HTML, "</table>")
    assert [s.name for s in _snapshot("services", cut).services] == ["custom_ssh", "Mosh"]
