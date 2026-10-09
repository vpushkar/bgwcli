"""Nonce, form and title extraction: attribute-order independent, scoped to the selected form, linear."""

from __future__ import annotations

import time

import pytest
from save_helpers import client_with, html

from bgwcli.client import extract_nonce, form_nonces, title_of
from bgwcli.errors import BgwError

TARGET = "/cgi-bin/dosprotect.ha"


def two_forms(first: str, second: str) -> str:
    return (
        f'<form action="/cgi-bin/other.ha">{first}</form>'
        f'<form action="{TARGET}">{second}<input type="submit" name="Save" value="Save"></form>'
    )


def test_a_value_before_name_nonce_in_the_selected_form_is_found():
    page = two_forms('<input name="nonce" value="aaaa01">', '<input type="hidden" value="bbbb01" name="nonce">')
    assert form_nonces(page, TARGET) == ["bbbb01"]


def test_attributes_between_value_and_name_and_single_quotes_are_found():
    page = two_forms("", "<input value='cccc02' class='x' id=n name='nonce'>")
    assert form_nonces(page, TARGET) == ["cccc02"]


def test_another_forms_nonce_is_never_collected():
    page = two_forms('<input name="nonce" value="aaaa01">', "<p>no nonce here</p>")
    assert form_nonces(page, TARGET) == []


def test_nonces_come_back_in_document_order():
    page = two_forms("", '<input name="nonce" value="aa11"><input value="bb22" name="nonce">')
    assert form_nonces(page, TARGET) == ["aa11", "bb22"]


def test_extract_nonce_accepts_either_attribute_order():
    assert extract_nonce('<input name="nonce" value="abc123">') == "abc123"
    assert extract_nonce('<input type="hidden" value="def456" name="nonce">') == "def456"
    assert extract_nonce('<input name="other" value="abc123">') is None


def test_a_post_sends_only_the_selected_forms_value_first_nonce():
    page = two_forms('<input name="nonce" value="aaaa01">', '<input type="hidden" value="bbbb01" name="nonce">')

    def handler(request, number):
        return html("<title>Done</title>") if request.method == "POST" else html(page)

    client, wire = client_with(handler)
    client.post_cgi_page("dosprotect", {"setting": "x"})
    posts = [r for r in wire.requests if r.method == "POST"]
    assert len(posts) == 1
    body = posts[0].body if isinstance(posts[0].body, str) else posts[0].body.decode()
    assert "nonce=bbbb01" in body and "aaaa01" not in body


def test_a_page_with_several_forms_and_no_target_form_sends_no_foreign_nonce():
    page = (
        '<form action="/cgi-bin/a.ha"><input name="nonce" value="aaaa01"></form>'
        '<form action="/cgi-bin/b.ha"><input name="nonce" value="bbbb01"></form>'
    )

    def handler(request, number):
        return html("<title>Done</title>") if request.method == "POST" else html(page)

    client, wire = client_with(handler)
    with pytest.raises(BgwError):
        client.post_cgi_page("dosprotect", {"setting": "x"})
    # No nonce for the target form: nothing is sent at all, and never another form's nonce.
    assert [r for r in wire.requests if r.method == "POST"] == []


def test_title_of_reads_the_first_title():
    assert title_of("<html><TITLE lang=en> Hello </TITLE></html>") == "Hello"
    assert title_of("<p>none</p>") == ""
    assert title_of("<title>unclosed") == ""


HOSTILE = {
    "forms": "<form>" * 20000,
    "forms-with-action": '<form action="/cgi-bin/dosprotect.ha">' * 20000,
    "inputs": '<input name="nonce" ' * 20000,
    "titles": "<title>" * 20000,
    "title-opens": "<title " * 20000,
    "angle": "<" * 20000,
    "closers": "</form>" * 20000,
}


@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_hostile_bodies_are_read_in_linear_time(shape):
    body = HOSTILE[shape]
    started = time.perf_counter()
    form_nonces(body, TARGET)
    extract_nonce(body)
    title_of(body)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("shape", sorted(HOSTILE))
def test_a_four_mebibyte_body_is_read_in_linear_time(shape):
    # The bound is relative to this machine (4x the data, 12x allowance) rather than an
    # absolute second, because the Raspberry Pi that runs the live suite is slow under load.
    unit = HOSTILE[shape][: len(HOSTILE[shape]) // 20000]

    def cost(size: int) -> float:
        body = unit * (size // len(unit))
        started = time.perf_counter()
        form_nonces(body, TARGET)
        extract_nonce(body)
        title_of(body)
        return time.perf_counter() - started

    small = max(cost(1024 * 1024), 1e-3)
    large = cost(4 * 1024 * 1024)
    assert large < small * 12, (small, large)


def test_doubling_a_hostile_body_roughly_doubles_the_work():
    def cost(count: int) -> float:
        body = "<form>" * count + '<input name="nonce" ' * count
        started = time.perf_counter()
        form_nonces(body, TARGET)
        extract_nonce(body)
        return time.perf_counter() - started

    small = max(cost(5000), 1e-4)
    large = cost(20000)
    assert large < small * 12, (small, large)
