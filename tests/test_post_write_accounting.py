"""A POST that failed before any byte could be written is still reported as sent and unanswered."""

from __future__ import annotations

import pytest
from save_helpers import client_with, form, html

from bgwcli.client import observe_post
from bgwcli.errors import RouterConnectionError


def test_a_connect_phase_failure_on_the_post_still_counts_as_an_unanswered_attempt():
    """The transport cannot say whether any bytes were written before it failed, so the accounting
    stays conservative on purpose: writeAttempted true, no response, one attempt."""

    def handler(request, number):
        if request.method == "POST":
            raise ConnectionRefusedError("connection refused")
        return html(form("dosprotect", "old"))

    client, wire = client_with(handler)
    with pytest.raises(RouterConnectionError, match="connection refused"), observe_post(client) as evidence:
        client.post_cgi_page("dosprotect", {"setting": "new"})
    assert sum(r.method == "POST" and "/login.ha" not in r.url for r in wire.requests) == 1
    assert evidence.attempted is True
    assert evidence.response_received is False
    assert evidence.attempts == 1
    assert evidence.status_code is None
