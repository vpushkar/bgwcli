"""A Set-Cookie that expires a cookie deletes it whatever its value is."""

from __future__ import annotations

import pytest
from save_helpers import client_with, html


@pytest.mark.parametrize("attributes", [
    "Max-Age=0", "max-age=-1", "Path=/; Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    "expires=Wed, 21 Oct 2015 07:28:00 GMT; Path=/", "Expires=Sun, 06 Nov 1994 08:49:37 GMT; Max-Age=0",
])
def test_an_expiring_set_cookie_deletes_the_cookie_even_with_a_value(attributes):
    client, _ = client_with(lambda *_: html(""))
    client._store_cookies(["sid=live; Path=/"])
    client._store_cookies([f"sid=stale; {attributes}"])
    assert "sid" not in client._cookies
    assert "sid=" not in client._cookie_header()


@pytest.mark.parametrize("error", [OSError("year out of range"), OverflowError("too large"), ValueError("bad")])
def test_an_expires_that_cannot_be_converted_is_absent_and_max_age_still_wins(monkeypatch, error):
    from bgwcli import client as client_module

    def refuse(_value):
        raise error

    monkeypatch.setattr(client_module, "parsedate_to_datetime", refuse)
    router, _ = client_with(lambda *_: html(""))
    router._store_cookies(["sid=kept; Expires=Fri, 31 Dec 9999 23:59:59 GMT"])
    assert router._cookies == {"sid": "kept"}
    router._store_cookies(["sid=gone; Expires=Fri, 31 Dec 9999 23:59:59 GMT; Max-Age=0"])
    assert "sid" not in router._cookies


@pytest.mark.parametrize("attributes", [
    "Max-Age=3600", "Max-Age=60; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    "Expires=Fri, 31 Dec 2099 23:59:59 GMT", "Path=/; HttpOnly", "Max-Age=garbage", "Expires=not a date",
])
def test_a_live_or_unparseable_expiry_keeps_the_cookie(attributes):
    client, _ = client_with(lambda *_: html(""))
    client._store_cookies([f"sid=fresh; {attributes}"])
    assert client._cookies == {"sid": "fresh"}
