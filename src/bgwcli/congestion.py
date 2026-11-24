"""The Wi-Fi congestion detection results on lanstatistics.ha.

After a detection the gateway renders, inside ``<div id="cong">``, a table whose first row is
``Time: <ctime>`` followed by a Channel / AP Count / Congestion Score header row and one row per
channel (observed live on fw 6.35.8, 2026-11-24). The results are kept per web session, so only the
session that ran the detection sees them. The generic lanstatistics view does not lay this table out.
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
from typing import Any

TIME_FORMAT = "%a %b %d %H:%M:%S %Y"


class _CongestionScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0  # div nesting inside <div id="cong">, 0 = outside
        self._in_table = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.rows: list[list[str]] = []
        self.found_section = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif dict(attrs).get("id") == "cong":
                self._depth = 1
                self.found_section = True
            return
        if not self._depth:
            return
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._depth:
            self._depth -= 1
        elif not self._depth:
            return
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_congestion(html: str) -> dict[str, Any] | None:
    """``{"time": "<ctime>", "channels": [{header: value, ...}, ...]}`` from the page's congestion
    section, or None when the section holds no results table (no detection ran in this session)."""
    scanner = _CongestionScanner()
    scanner.feed(html)
    scanner.close()
    rows = scanner.rows
    time = None
    if rows and len(rows[0]) == 1 and rows[0][0].lower().startswith("time:"):
        time = rows[0][0].split(":", 1)[1].strip()
        rows = rows[1:]
    if len(rows) < 2:
        return None
    header, data = rows[0], rows[1:]
    channels = [dict(zip(header, row, strict=False)) for row in data if len(row) == len(header)]
    if not channels:
        return None
    return {"time": time, "channels": channels}


def congestion_time(results: dict[str, Any]) -> datetime | None:
    """The detection's local time as the gateway prints it, or None when absent or unreadable."""
    try:
        return datetime.strptime(results.get("time") or "", TIME_FORMAT)
    except ValueError:
        return None
