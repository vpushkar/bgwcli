"""Port of tests/router-fixtures.test.ts.

The sanitized router fixture pack (tests/fixtures/{router-html,parsed,expected}) is generated
locally and gitignored. When it is absent (or incomplete) every test here skips; when present,
parse_page must reproduce the saved parser output for every mapped page and the saved
usefulness/redaction receipts must hold.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from bgwcli.pages import mapped_pages
from bgwcli.parser import parse_page
from bgwcli.types import ParsedPage, to_json_dict

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
HTML_DIR = FIXTURE_ROOT / "router-html"
PARSED_DIR = FIXTURE_ROOT / "parsed"
EXPECTED_DIR = FIXTURE_ROOT / "expected"
MAPPED_PAGES = mapped_pages()

_LEAK = re.compile(
    r'"value":"(?!\[redacted\])[^"]+"[^{}]{0,160}"sensitive":true|\b[a-f0-9]{32}\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b',
    re.IGNORECASE,
)


def _fixture_pages(directory: Path, suffix: str) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.name[: -len(suffix)] for path in directory.iterdir() if path.name.endswith(suffix))


def _pack_complete() -> bool:
    return (
        len(_fixture_pages(HTML_DIR, ".html")) == len(MAPPED_PAGES)
        and len(_fixture_pages(PARSED_DIR, ".json")) == len(MAPPED_PAGES)
        and len(_fixture_pages(EXPECTED_DIR, ".json")) == len(MAPPED_PAGES)
    )


PACK_COMPLETE = _pack_complete()
requires_pack = pytest.mark.skipif(
    not PACK_COMPLETE,
    reason="router fixture pack not present; capture it with the TS `bun run fixtures:capture` "
    "(BGW_ACCESS_CODE set) and copy tests/fixtures here",
)


def parsed_data_count(parsed: ParsedPage) -> int:
    return (
        len(parsed.values)
        + len(parsed.tables)
        + len(parsed.fields)
        + len(parsed.selects)
        + len(parsed.textareas)
        + len(parsed.buttons)
        + len(parsed.forms)
    )


def _has_useful_fields(parsed: ParsedPage) -> bool:
    return any(f.name not in ("nonce", "hashpassword") for f in parsed.fields)


def _has_useful_tables(parsed: ParsedPage) -> bool:
    return len(parsed.tables) > 0 and len({key for row in parsed.tables for key in row}) > 0


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@requires_pack
def test_router_fixture_pack_covers_every_mapped_page():
    assert _fixture_pages(HTML_DIR, ".html") == MAPPED_PAGES
    assert _fixture_pages(PARSED_DIR, ".json") == MAPPED_PAGES
    assert _fixture_pages(EXPECTED_DIR, ".json") == MAPPED_PAGES


@requires_pack
@pytest.mark.parametrize("page", MAPPED_PAGES)
def test_router_fixture_parses(page: str):
    html = (HTML_DIR / f"{page}.html").read_text(encoding="utf-8")
    parsed = parse_page(page, html)
    data = to_json_dict(parsed)
    saved = _read_json(PARSED_DIR / f"{page}.json")
    expected = _read_json(EXPECTED_DIR / f"{page}.json")

    assert data == saved
    assert expected["page"] == page
    assert (parsed_data_count(parsed) > 0) == expected["dataObtainable"]
    assert _has_useful_fields(parsed) == expected["usefulFieldsExist"]
    assert _has_useful_tables(parsed) == expected["usefulTablesExist"]
    assert (len(parsed.buttons) > 0) == expected["buttonsDiscovered"]
    assert (len(parsed.forms) > 0) == expected["formsDiscovered"]
    assert expected["secretsRedacted"] is True
    if expected["pageLoads"]:
        assert expected["notOnlyJunk"] == (parsed_data_count(parsed) > 0)
    else:
        assert expected["notOnlyJunk"] is False
    assert expected["counts"] == {
        "values": len(parsed.values),
        "valueEntries": len(parsed.value_entries or []),
        "tables": len(parsed.tables),
        "fields": len(parsed.fields),
        "selects": len(parsed.selects),
        "textareas": len(parsed.textareas),
        "buttons": len(parsed.buttons),
        "forms": len(parsed.forms),
        "links": len(parsed.links or []),
    }
    assert expected["valueKeys"] == sorted(parsed.values)
    assert expected["tableColumns"] == sorted({key for row in parsed.tables for key in row})
    assert expected["fieldNames"] == sorted(f.name for f in parsed.fields)
    assert expected["selectNames"] == sorted(s.name for s in parsed.selects)
    assert expected["textareaNames"] == sorted(t.name for t in parsed.textareas)
    assert expected["buttonNames"] == sorted(b.name for b in parsed.buttons)
    assert expected["formActions"] == sorted({f.action for f in parsed.forms})
    assert expected["linkTargets"] == sorted({link.href for link in parsed.links or []})
    assert expected["valueEntryLabels"] == sorted({e.label for e in parsed.value_entries or []})
    assert not _LEAK.search(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


def test_fixture_pack_skip_logic_is_consistent():
    """Sanity: the skip decision reflects the directories actually on disk."""
    assert (
        _fixture_pages(HTML_DIR, ".html") == MAPPED_PAGES
        and _fixture_pages(PARSED_DIR, ".json") == MAPPED_PAGES
        and _fixture_pages(EXPECTED_DIR, ".json") == MAPPED_PAGES
    ) == PACK_COMPLETE
