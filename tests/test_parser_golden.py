"""Parser output over a fixed corpus is byte-for-byte what it was before the parser gained resource
bounds: the bounds only act on hostile bodies, never on pages shaped like the router's.

The corpus is the committed synthetic router pages, every HTML constant the integration tests build,
and a set of hand-written layouts (spans, nested tables, band headings, unclosed markup). The
recorded JSON lives in tests/fixtures-parser-golden/corpus.json; set BGWCLI_REGENERATE_PARSER_GOLDEN=1
to rewrite it after an intended parser change. A captured live fixture pack (tests/fixtures, gitignored)
is compared too when it is present and has a recording.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import integration_html
import pytest

from bgwcli.parser import parse_page
from bgwcli.types import to_json_dict

TESTS = Path(__file__).parent
GOLDEN = TESTS / "fixtures-parser-golden" / "corpus.json"

HAND_WRITTEN = {
    "colspan-header": (
        "<table><tr><th colspan=2>Name</th><th>Value</th></tr><tr><td>a</td><td>b</td><td>c</td></tr>"
        "<tr><td colspan=2>wide</td><td>d</td></tr></table>"
    ),
    "rowspan-label": (
        "<table><tr><td rowspan=2>Password</td><td>one</td></tr><tr><td>two</td></tr>"
        "<tr><td>Channel</td><td>6</td></tr></table>"
    ),
    "nested-tables": (
        "<h2>Outer</h2><table><tr><td>Label</td><td><table><tr><td>Inner</td><td>1</td></tr></table></td></tr>"
        "<tr><td>Password</td><td>s3cret</td></tr></table>"
    ),
    "band-headings": (
        "<h2>2.4 GHz</h2><table><tr><td>Current Channel</td><td>6 (20 MHz)</td></tr></table>"
        "<h2>5 GHz</h2><table><tr><td>Current Channel</td><td>36 (80 MHz)</td></tr></table>"
    ),
    "unclosed-markup": (
        "<h1>Status Currently Up<table><tr><td>A<td>B<tr><td>C<td>D<ul><li>x<li>y</ul>"
        "<select name=s><option>1<option selected>2</select><p>para<div class=desc>Describe</div>"
    ),
    "wide-table-with-th": (
        "<table><tr><th>Name</th><th>Status</th><th>Passphrase</th></tr>"
        "<tr><td>a</td><td>up</td><td>k</td></tr><tr><td>b</td><td>down</td><td>m</td></tr></table>"
    ),
    "stray-end-tags": "<div><b>text</i></b></span></div></p><table><tr><td>k</td><td>v</td></tr></table></td>",
    "secret-label-controls": (
        '<table><tr><td>Wi-Fi Password</td><td><input name="x1" value="s3"></td></tr>'
        '<tr><td>Note</td><td><input name="x2" value="ok"></td></tr></table>'
    ),
}


TRICKY = [
    "<textarea name=a><b>x</b> &amp; y</textarea>",
    "<title>A &amp; <b>B</title>",
    "<script>if(a<b){}</script><p>x",
    "<!-- c --><td>x",
    "<!--> x",
    "<input name=a value='&quot;x&quot;'>",
    "<select name=a><option>1<option selected>2</select>",
    "<table><tr><td>a</td></tr></table></table>",
    "<![CDATA[x]]><td>x",
    "<?php x ?><td>y",
    "<td>a&nbsp;b &#x41; &bogus; &amp</td>",
    "<style>td{}</style><textarea name=t></textarea>x",
    "<textarea name=t><!-- x --></textarea>",
    "<title><!-- x --></title>",
    "<plaintext><td>x",
    "<script>document.write('</scr'+'ipt>')</script><td>x",
]
HAND_WRITTEN.update({f"tricky-{index:02d}": html for index, html in enumerate(TRICKY)})


def corpus() -> dict[str, tuple[str, str]]:
    """name -> (page id, html)"""
    items: dict[str, tuple[str, str]] = {}
    for path in sorted((TESTS / "fixtures-synthetic").glob("*.html")):
        items[f"synthetic/{path.stem}"] = (path.stem, path.read_text(encoding="utf-8"))
    for name in sorted(vars(integration_html)):
        value = getattr(integration_html, name)
        if name.endswith("_HTML") and isinstance(value, str):
            match = re.search(r"/cgi-bin/(\w+)\.ha", value)
            items[f"integration/{name}"] = (match.group(1) if match else "x", value)
    for name, value in HAND_WRITTEN.items():
        items[f"hand/{name}"] = ("sysinfo" if name.startswith("tricky") else "x", value)
    live = TESTS / "fixtures" / "router-html"
    if live.is_dir():
        for path in sorted(live.glob("*.html")):
            items[f"live/{path.stem}"] = (path.stem, path.read_text(encoding="utf-8"))
    return items


def render(page: str, html: str) -> dict:
    return {
        "redacted": to_json_dict(parse_page(page, html)),
        "secrets": to_json_dict(parse_page(page, html, include_secrets=True)),
    }


def test_corpus_parses_exactly_as_recorded():
    recorded = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    current = {name: render(page, html) for name, (page, html) in corpus().items()}
    if os.environ.get("BGWCLI_REGENERATE_PARSER_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps({k: v for k, v in current.items() if not k.startswith("live/")}, indent=1) + "\n")
        pytest.skip("golden regenerated")
    for name, rendered in current.items():
        if name.startswith("live/") and name not in recorded:
            continue
        assert name in recorded, f"{name} has no recording"
        assert rendered == recorded[name], f"{name} no longer parses as recorded"
    assert {name for name in recorded} <= set(current)
