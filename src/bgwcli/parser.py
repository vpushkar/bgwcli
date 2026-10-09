"""Router page parsers built on a light DOM produced by ``html.parser.HTMLParser``.

The TS CLI scanned raw HTML with regular expressions; this module builds a small element tree
instead and derives the same ``ParsedPage`` shape from it (same fields, selects, buttons, forms,
tables, values, value entries and links for the same markup). Behavior is pinned by
tests/test_parser.py, which carries every expectation the TS test-suite had for the parser.
"""

from __future__ import annotations

import heapq
import json
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any, Generic, TypeVar
from urllib.parse import unquote

from .devices import split_ip_and_name
from .errors import BgwError
from .html import normalize_whitespace
from .redact import REDACTED, is_sensitive_name, redact_value
from .types import (
    Device,
    LogEntry,
    ParsedButton,
    ParsedField,
    ParsedForm,
    ParsedLink,
    ParsedPage,
    ParsedSelect,
    ParsedTextarea,
    ParsedValueEntry,
    SelectOption,
    SitemapEntry,
)

Record = dict[str, str]


class TruncatedPageError(BgwError):
    """A reader that needs the whole page was given one the parser cut at a bound (see ``ParsedPage.truncated``):
    what it would report is partial, so it reports this instead."""

BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
CONTROL_TAGS = frozenset({"input", "select", "textarea", "button"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_TABLE_SECTION_TAGS = frozenset({"table", "thead", "tbody", "tfoot"})
_P_CLOSERS = frozenset(
    {"p", "div", "table", "form", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "blockquote", "hr"}
)
_SKIP_TEXT_TAGS = frozenset({"script", "style"})
# Elements whose content is text, never markup: script/style are raw text, textarea/title are RCDATA
# (character references decoded). The tree builder reads them itself so the result does not depend
# on how a given Python release's HTMLParser tokenises them.
_RAW_TEXT_TAGS = frozenset({"script", "style"})
_RCDATA_TAGS = frozenset({"textarea", "title"})
_RAW_CLOSE = {
    tag: re.compile(rf"</{tag}(?=[\s/>])", re.IGNORECASE) for tag in _RAW_TEXT_TAGS | _RCDATA_TAGS
}
_HEADING_TAGS = frozenset({"h1", "h2", "h3"})
_SECTION_BREAKS = frozenset({"table", "h1", "h2", "h3"})

_CGI_PAGE = re.compile(r"/cgi-bin/([^/?#]+)\.ha", re.IGNORECASE)
_SITEMAP_HREF = re.compile(r".*/cgi-bin/(.+?)\.ha", re.IGNORECASE)
# Page headings split into a value only on the capitalised word (TS parity: "WAN is currently down" is
# prose, not a key/value); fiber threshold headings are matched case-insensitively.
_CURRENTLY = re.compile(r"\s+Currently\s+")
_FIBER_CURRENTLY = re.compile(r"\s+Currently\s+", re.IGNORECASE)
_DEFAULT_LABEL = re.compile(r"^(.*?\bDefault):\s*(.+)$", re.IGNORECASE)
_DESC_CLASS = re.compile(r"\bdesc\b")
_BAND_HEADING = re.compile(r"^((?:2\.4|5)\s*GHz)", re.IGNORECASE)
_CHANNEL_WIDTH = re.compile(r"(\d+)\s*\(([^)]+)\)")
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)
_LOGIN_ACTION = re.compile(r"/cgi-bin/login\.ha$", re.IGNORECASE)
_ACCESS_CODE_REQUIRED = re.compile(r"Access Code Required", re.IGNORECASE)
_LOGIN_TITLE = re.compile(r"^Login$", re.IGNORECASE)


# --- DOM-lite -----------------------------------------------------------------------------------


@dataclass(eq=False)
class Element:
    """One HTML element. ``start``/``end`` are document-order counters (start tag / end tag events)."""

    tag: str
    attrs: dict[str, str]
    start: int
    end: int = -1
    children: list[Element | str] = field(default_factory=list)
    # Set on the document root only: every element by tag, in document order, so a whole-document
    # ``find_all`` is a lookup rather than another walk of the tree.
    by_tag: dict[str, list[Element]] | None = field(default=None, repr=False)
    # Set on the document root only: the document had more than MAX_ELEMENTS elements and was cut, or
    # tables/headings nested past MAX_NESTING were dropped.
    truncated: bool = field(default=False, repr=False)
    # Set when ``text()`` stopped at the text bound with more text beneath this element.
    text_cut: bool = field(default=False, repr=False)

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name)

    def has(self, name: str) -> bool:
        return name in self.attrs

    def iter_elements(self, *, stop: frozenset[str] = frozenset()) -> Iterator[Element]:
        """Depth-first descendants in document order; subtrees rooted at a ``stop`` tag are yielded but not entered.

        Iterative (an explicit stack of child iterators) so unclosed or deeply nested router markup
        cannot hit Python's recursion limit.
        """
        stack: list[Iterator[Element | str]] = [iter(self.children)]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
                continue
            if isinstance(child, Element):
                yield child
                if child.tag not in stop:
                    stack.append(iter(child.children))

    def find_all(self, tag: str, *, stop: frozenset[str] = frozenset()) -> list[Element]:
        if self.by_tag is not None and not stop:
            return list(self.by_tag.get(tag, ()))
        return [el for el in self.iter_elements(stop=stop) if el.tag == tag]

    def text(self, replacements: dict[int, str] | None = None) -> str:
        """Text content with every tag boundary rendered as a space, whitespace normalized (TS stripTags).

        ``replacements`` maps ``id(descendant)`` to the text written in place of that descendant's
        own text (used to keep a nested secret redacted inside its enclosing cell)."""
        parts: list[str] = []
        size = 0
        for part in self._walk_text(boundaries=True, replacements=replacements or {}):
            # An element's text is read for display; nested hostile markup could otherwise make every
            # enclosing cell re-read everything beneath it. ``text_cut`` records that more was there.
            if len(parts) >= MAX_TEXT_PARTS or size >= MAX_TEXT_CHARS:
                self.text_cut = True
                break
            if size + len(part) > MAX_TEXT_CHARS:
                self.text_cut = True
            parts.append(part[: MAX_TEXT_CHARS - size])
            size += len(part)
        return normalize_whitespace("".join(parts))

    def raw_text(self) -> str:
        """Descendant text exactly as written (no whitespace collapse, no tag-boundary spaces), minus the
        single newline HTML drops right after a ``<textarea>`` start tag. Used for form data."""
        text = "".join(self._walk_text(boundaries=False, replacements={}))
        return text[2:] if text.startswith("\r\n") else text[1:] if text.startswith(("\n", "\r")) else text

    def _walk_text(self, *, boundaries: bool, replacements: dict[int, str]) -> Iterator[str]:
        """Descendant text in document order (script/style skipped), iteratively; with ``boundaries``
        every child element is wrapped in single spaces. A descendant listed in ``replacements``
        contributes the replacement text instead of its own."""
        stack: list[Iterator[Element | str]] = [iter(self.children)]
        while stack:
            child = next(stack[-1], _END)
            if child is _END:
                stack.pop()
                if boundaries and stack:
                    yield " "
                continue
            if isinstance(child, str):
                yield child
                continue
            if boundaries:
                yield " "
            if child.tag in _SKIP_TEXT_TAGS or id(child) in replacements:
                yield replacements.get(id(child), "")
                if boundaries:
                    yield " "
                continue
            stack.append(iter(child.children))


_END = Element("#end", {}, -1)
# Read in front of a cell's text when the text bound cut it, so label and header detection treat it as secret-named.
_UNREAD_TEXT_MARKER = "Password"


class _StopParsing(Exception):
    """Raised by the tree builder to end a feed that has reached ``MAX_ELEMENTS``."""


class RawTextHTMLParser(HTMLParser):
    """An ``HTMLParser`` that reads the content of script, style, textarea and title itself, so the
    tokens do not depend on the Python release (3.14 keeps textarea and title markup as text, earlier
    releases parse it): the content, up to the matching end tag or the end of the document, is one text
    node, character references decoded for textarea and title. A subclass's ``handle_starttag`` calls
    ``expect_raw_content`` for the tag it has just opened."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._raw_tag: str | None = None

    def expect_raw_content(self, tag: str) -> None:
        if tag in _RAW_TEXT_TAGS or tag in _RCDATA_TAGS:
            self._raw_tag = tag

    def set_cdata_mode(self, *args: Any, **kwargs: Any) -> None:
        """The stdlib's own raw-text modes are off (they differ between Python releases): see parse_starttag."""

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self._raw_tag = None  # a self-closed element has no content
        self.handle_endtag(tag)

    def parse_starttag(self, i: int) -> int:
        """Parse a start tag; a script/style/textarea/title start tag also consumes its content up to the
        matching end tag (or the end of the document) and delivers it as one text node."""
        self._raw_tag = None
        end = super().parse_starttag(i)
        tag, self._raw_tag = self._raw_tag, None
        if end < 0 or tag is None:
            return end
        match = _RAW_CLOSE[tag].search(self.rawdata, end)
        stop = match.start() if match else len(self.rawdata)
        content = self.rawdata[end:stop]
        if content:
            self.handle_data(unescape(content) if tag in _RCDATA_TAGS else content)
        return stop


class _TreeBuilder(RawTextHTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = Element("#document", {}, 0)
        self.elements: list[Element] = []
        self._stack: list[Element] = [self.root]
        self._counter = 0
        # How many elements of each tag are open: a stray end tag (or an implicit close with nothing
        # to close) is then decided in O(1) instead of by scanning the whole open stack.
        self._open: dict[str, int] = {}
        self._open_headings = 0
        # A table or heading was dropped for nesting past MAX_NESTING.
        self.nesting_cut = False

    @property
    def _current(self) -> Element:
        return self._stack[-1]

    def _tick(self) -> int:
        self._counter += 1
        return self._counter

    def _close_nearest(self, targets: frozenset[str], barriers: frozenset[str]) -> None:
        if not any(self._open.get(tag) for tag in targets):
            return
        for index in range(len(self._stack) - 1, 0, -1):
            tag = self._stack[index].tag
            if tag in targets:
                self._pop_to(index)
                return
            if tag in barriers:
                return

    def _pop_to(self, index: int) -> None:
        while len(self._stack) > index:
            element = self._stack.pop()
            element.end = self._tick()
            self._open[element.tag] -= 1
            if element.tag in _HEADING_TAGS:
                self._open_headings -= 1

    def _implicit_close(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._close_nearest(frozenset({"td", "th"}), frozenset({"tr", "table"}))
        elif tag == "tr":
            self._close_nearest(frozenset({"tr"}), _TABLE_SECTION_TAGS)
        elif tag == "option":
            self._close_nearest(frozenset({"option"}), frozenset({"select"}))
        elif tag == "select":
            # Selects do not nest: a select start tag inside an open select ends that select, so an
            # unclosed select cannot swallow the next one's options.
            self._close_nearest(frozenset({"select"}), frozenset({"form", "table"}))
        elif tag == "li":
            self._close_nearest(frozenset({"li"}), frozenset({"ul", "ol"}))
        if tag in _P_CLOSERS and self._current.tag == "p":
            self._pop_to(len(self._stack) - 1)

    def _open_element(self, tag: str, attrs: list[tuple[str, str | None]]) -> Element | None:
        """Start ``tag``; None when it is dropped because tables or headings are already open
        ``MAX_NESTING`` deep (the rest of that markup is then text of the enclosing element)."""
        if len(self.elements) >= MAX_ELEMENTS:
            raise _StopParsing
        if (tag == "table" and self._open.get("table", 0) >= MAX_NESTING) or (
            tag in _HEADING_TAGS and self._open_headings >= MAX_NESTING
        ):
            self.nesting_cut = True
            return None
        self._implicit_close(tag)
        # Attribute values stay raw (a value posted back must round-trip byte for byte); display
        # paths normalise whitespace, U+00A0 included, where they read an attribute.
        element = Element(tag, {name: value or "" for name, value in attrs}, self._tick())
        self._current.children.append(element)
        self.elements.append(element)
        if tag in _VOID_TAGS:
            element.end = element.start
        else:
            self._stack.append(element)
            self._open[tag] = self._open.get(tag, 0) + 1
            if tag in _HEADING_TAGS:
                self._open_headings += 1
        return element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open_element(tag, attrs) is not None:
            self.expect_raw_content(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open_element(tag, attrs) is not None and tag not in _VOID_TAGS:
            self._pop_to(len(self._stack) - 1)

    def handle_endtag(self, tag: str) -> None:
        if not self._open.get(tag):
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                self._pop_to(index)
                return

    def handle_data(self, data: str) -> None:
        self._current.children.append(data)

    def finish(self) -> Element:
        self.close()
        self._pop_to(1)
        self.root.end = self._tick()
        by_tag: dict[str, list[Element]] = {}
        for element in self.elements:
            by_tag.setdefault(element.tag, []).append(element)
        self.root.by_tag = by_tag
        return self.root


def parse_document(html: str) -> Element:
    """Parse HTML into an element tree rooted at a synthetic ``#document`` element."""
    builder = _TreeBuilder()
    truncated = False
    try:
        builder.feed(html)
    except _StopParsing:
        truncated = True
        builder.rawdata = ""  # the rest of the document is not read
    root = builder.finish()
    root.truncated = truncated or builder.nesting_cut
    return root


# --- table helpers --------------------------------------------------------------------------------


def _table_rows(table: Element, *, keep_empty: bool = False) -> list[list[Element]]:
    """Cells of each row of ``table`` (nested tables excluded); rows without cells are dropped unless
    ``keep_empty``: the column grid keeps them, because a rowspan cell reaches over them as it does in a
    browser."""
    rows: list[list[Element]] = []
    for tr in table.find_all("tr", stop=frozenset({"table"})):
        cells = [el for el in tr.iter_elements(stop=frozenset({"table", "tr"})) if el.tag in ("td", "th")]
        if cells or keep_empty:
            rows.append(cells)
    return rows


# Resource bounds for hostile bodies (the client caps a body at 4 MiB). No router page comes near them.
MAX_SPAN = 64  # a colspan/rowspan above this is read as this
MAX_GRID_CELLS = 250_000  # rows x columns laid out for one table; further rows are dropped
MAX_NESTING = 256  # open <table> elements, and open headings, at once; deeper markup stops being structure
MAX_ELEMENTS = 150_000  # elements in one document; the rest of a larger one is not read
MAX_TEXT_PARTS = 5_000  # text pieces and element boundaries read into one element's text
MAX_TEXT_CHARS = 100_000  # characters read into one element's text
Cell = TypeVar("Cell")


def raw_span_value(value: str | None) -> int:
    """A colspan/rowspan attribute as a positive int, uncapped (missing, invalid or 0 -> 1; a run of
    digits too long to convert counts as over the cap)."""
    text = (value or "1").strip()
    try:
        span = int(text)
    except ValueError:
        return MAX_SPAN + 1 if text.isdigit() else 1
    return max(span, 1)


def span_value(value: str | None) -> int:
    """A colspan/rowspan attribute as a positive int (missing, invalid or 0 -> 1, capped)."""
    return min(raw_span_value(value), MAX_SPAN)


def _span(cell: Element, name: str) -> int:
    return raw_span_value(cell.attr(name))


@dataclass
class TableGridRow(Generic[Cell]):
    """One table row laid out on the column grid. ``columns`` holds one cell per column (a colspan
    cell repeated, a rowspan cell carried down from the row it starts in); ``span_copies`` are the
    column indexes holding a colspan repeat; ``continued`` is True when an earlier row's rowspan
    cell reaches into this row. ``cells`` are the row's own cells as written."""

    cells: list[Cell]
    columns: list[Cell]
    span_copies: frozenset[int]
    continued: bool
    # A cell of this row has a span above MAX_SPAN, read as MAX_SPAN: the columns after it, and the rows
    # an over-long rowspan reaches, are not where a browser puts them.
    clamped: bool = False


def layout_table(rows: list[list[Cell]], span: Callable[[Cell, str], int]) -> list[TableGridRow[Cell]]:
    """Lay rows of cells out on the column grid; ``span(cell, "colspan" | "rowspan")`` reads a span.

    A rowspan cell carried into later rows keeps its colspan-repeat marks, so a row an earlier cell
    spans both ways is as excluded as the row it starts in.

    Shared by the parser and the fixture sanitizer so both read a spanned table the same way.
    """
    return layout_table_bounded(rows, span, None)[0]


def layout_table_bounded(
    rows: list[list[Cell]], span: Callable[[Cell, str], int], max_cells: int | None
) -> tuple[list[TableGridRow[Cell]], bool]:
    """``layout_table`` that stops once the grid would hold more than ``max_cells`` cells (rows x
    columns); the row that crosses the bound and every later row are dropped. The flag is True
    when rows were dropped."""
    grid: list[TableGridRow[Cell]] = []
    carries: dict[int, list[Any]] = {}  # column -> [cell, rows still to cover, is a colspan repeat]
    total = 0
    for cells in rows:
        columns: list[Cell] = []
        copies: set[int] = set()
        continued = False
        clamped = False
        for cell in cells:
            continued = _take_carries(columns, carries, copies) or continued
            rowspan, colspan = span(cell, "rowspan"), span(cell, "colspan")
            if rowspan > MAX_SPAN or colspan > MAX_SPAN:
                clamped = True
                rowspan, colspan = min(rowspan, MAX_SPAN), min(colspan, MAX_SPAN)
            for index in range(colspan):
                if index:
                    copies.add(len(columns))
                if rowspan > 1:
                    carries[len(columns)] = [cell, rowspan - 1, index > 0]
                columns.append(cell)
            if max_cells is not None and total + len(columns) > max_cells:
                return grid, True
        continued = _take_carries(columns, carries, copies) or continued
        total += len(columns)
        if max_cells is not None and total > max_cells:
            return grid, True
        row = TableGridRow(
            cells=cells, columns=columns, span_copies=frozenset(copies), continued=continued, clamped=clamped
        )
        grid.append(row)
    return grid, False


def _take_carries(columns: list[Any], carries: dict[int, list[Any]], copies: set[int]) -> bool:
    """Append rowspan cells that reach the next column of ``columns``; True when any did."""
    took = False
    while len(columns) in carries:
        column = len(columns)
        carry = carries[column]
        if carry[2]:
            copies.add(column)
        columns.append(carry[0])
        took = True
        carry[1] -= 1
        if carry[1] <= 0:
            del carries[column]
    return took


def column_headers(texts: list[str], span_copies: frozenset[int]) -> list[str]:
    """Header names for a laid-out header row: a colspan header names every column it covers
    ("Name", "Name 2", ...)."""
    headers: list[str] = []
    repeat = 1
    for index, text in enumerate(texts):
        repeat = repeat + 1 if index in span_copies else 1
        headers.append(f"{text} {repeat}" if repeat > 1 and text else text)
    return headers


def _secret_label(text: str) -> tuple[str | None, str | None]:
    """``(label, held value)`` for a first-cell text. ``label`` is None unless the text names a
    secret (``Password``, ``Phone Number``); the held value is what the label cell itself carries
    after a colon (``Password Default: abc``, ``Access Code: abc``), None when it carries none."""
    label, held = split_default_label(re.sub(r":$", "", text))
    if held is None:
        head, colon, tail = text.partition(":")
        if colon and tail.strip():
            label, held = normalize_whitespace(head), normalize_whitespace(tail)
    if not is_sensitive_name(label):
        return None, None
    return label, None if held in (None, "", REDACTED) else held


def _first_row_names_columns(
    first: TableGridRow[Cell], text: Callable[[Cell], str], is_header_cell: Callable[[Cell], bool] | None
) -> bool:
    """True when the first row of a table can name its columns: any row of a table wider than two
    columns, or a narrower table's row that has a header cell, or that is headed by a plain word
    above a secret-named column (``Name | Password``) rather than labelling a value."""
    if len(first.columns) > 2:
        return True
    # A row made only of header cells names columns; a header cell beside a data cell is a row header
    # (``<th>Key</th><td>value</td>``), a label.
    if is_header_cell is not None and all(is_header_cell(cell) for cell in first.cells):
        return True
    return (
        len(first.columns) == 2
        and _secret_label(text(first.columns[0]))[0] is None
        and is_sensitive_name(text(first.columns[1]))
    )


def _names_a_secret(grid: list[TableGridRow[Cell]], text: Callable[[Cell], str]) -> bool:
    """True when a header-row cell or a first-column cell of the table is secret-named."""
    first = next((row for row in grid if row.cells), grid[0])
    named = [*first.cells, *(row.columns[0] for row in grid if row.columns)]
    return any(is_sensitive_name(text(cell)) for cell in named)


def sensitive_table_cells(
    grid: list[TableGridRow[Cell]],
    text: Callable[[Cell], str],
    *,
    header_row_is_data: bool = False,
    is_header_cell: Callable[[Cell], bool] | None = None,
) -> list[tuple[Cell, str]]:
    """The cells of a laid-out table that hold a secret, as ``(cell, replacement)`` (see ``govern_table``)."""
    return govern_table(grid, text, header_row_is_data=header_row_is_data, is_header_cell=is_header_cell)[0]


def secret_label_cells(
    grid: list[TableGridRow[Cell]],
    text: Callable[[Cell], str],
    *,
    header_row_is_data: bool = False,
    is_header_cell: Callable[[Cell], bool] | None = None,
) -> list[Cell]:
    """The label cells of a laid-out table whose text names a secret (see ``govern_table``)."""
    return govern_table(grid, text, header_row_is_data=header_row_is_data, is_header_cell=is_header_cell)[1]


def govern_table(
    grid: list[TableGridRow[Cell]],
    text: Callable[[Cell], str],
    *,
    header_row_is_data: bool = False,
    is_header_cell: Callable[[Cell], bool] | None = None,
) -> tuple[list[tuple[Cell, str]], list[Cell]]:
    """``(cells that hold a secret as (cell, replacement), label cells that name a secret)`` of a laid-out table.

    Every value cell of a row whose label (first column) names a secret, however many value columns
    the row has and whether the label is the row's own cell or a rowspan cell carried down from an
    earlier row; the text a secret label cell holds itself (``Access Code Default: <value>``, which
    keeps the label); and, in a table wider than two columns, every cell under a secret header
    (a colspan header names each column it covers). The first row of such a wide table is its
    header row and names columns rather than labelling values, unless it has no header cell
    (``is_header_cell``) and its first cell names a secret: that is a labelled data row, and its
    values are governed as such. A caller that must fail closed (the fixture sanitizer) passes
    ``header_row_is_data`` to govern the first row as a labelled row too. ``text(cell)`` reads a
    cell's text. A label cell that names a secret is reported even when it holds no secret text itself
    (a control beside the label word is the secret there). Shared by the parser and the fixture sanitizer.
    """
    found: dict[int, tuple[Cell, str]] = {}
    labels: dict[int, Cell] = {}

    def add(cell: Cell, replacement: str) -> None:
        found.setdefault(id(cell), (cell, replacement))

    first = next((index for index, row in enumerate(grid) if row.cells), None)
    if first is None:
        return [], []
    names_columns = _first_row_names_columns(grid[first], text, is_header_cell)
    headers = (
        column_headers([text(cell) for cell in grid[first].columns], grid[first].span_copies) if names_columns else []
    )
    for index, row in enumerate(grid):
        if not row.cells:
            continue
        label_cell = row.columns[0]
        header_row = names_columns and index == first and not header_row_is_data
        if header_row and (
            (is_header_cell is not None and not any(is_header_cell(cell) for cell in row.cells))
            or len(row.columns) <= 2  # a narrow row reads as a label and its value as well as a header
        ):
            header_row = _secret_label(text(label_cell))[0] is None
        label, held = (None, None) if header_row else _secret_label(text(label_cell))
        if label is not None:
            labels.setdefault(id(label_cell), label_cell)
            if held is not None:
                add(label_cell, f"{label}: {REDACTED}")
            for cell in row.columns[1:]:
                if cell is not label_cell:
                    add(cell, REDACTED)
        if names_columns and index > first:
            for header, cell in zip(headers, row.columns, strict=False):
                if is_sensitive_name(header):
                    add(cell, REDACTED)
    if any(row.clamped for row in grid) and _names_a_secret(grid, text):
        # A span above the bound was read as the bound, so cells no longer sit under the column or beside
        # the label a browser puts them at: every cell of a table that names a secret is governed.
        for row in grid:
            for cell in (*row.cells, *row.columns):
                add(cell, REDACTED)
    return list(found.values()), list(labels.values())


def _is_header_cell(cell: Element) -> bool:
    return cell.tag == "th"


class _Doc:
    """A parsed document plus what every outward text view needs: the laid-out tables, memoised cell
    text, and (unless secrets are included) the replacement text of every element that holds a
    secret, built once for the whole document. ``text()`` is the one way a view reads element text,
    so a secret stays redacted in every enclosing cell, description, heading and link."""

    def __init__(self, root: Element, include_secrets: bool) -> None:
        self.root = root
        self.include_secrets = include_secrets
        self.tables = root.find_all("table")
        self._grids: dict[int, list[TableGridRow[Element]]] = {}
        # The document or a table's grid hit its bound (MAX_ELEMENTS, MAX_GRID_CELLS) and was cut short.
        self.truncated = root.truncated
        # table id -> document position where its laid-out rows end, for a table whose grid was cut
        self._cut_tables: dict[int, tuple[Element, int]] = {}
        self._plain: dict[int, str] = {}
        # Cells that name a secret as a label: a control inside one is the secret.
        self._label_cells: list[Element] = []
        secret_cells = self._secret_cells()
        # Everything inside a value cell of a secret-labelled row is secret, even a cell with no text of
        # its own: a control whatever it is named, an icon link whatever its title says. So is everything
        # past the rows the parser could lay out: a bound never lets unread markup through unredacted.
        self.secret_scope: frozenset[int] = self._scope_of(
            [cell for cell, replacement in secret_cells if replacement == REDACTED] + self._label_cells
        ) | self._unread_tail()
        self.replacements: dict[int, str] = {} if include_secrets else self._secret_replacements(secret_cells)
        # Everything inside a redacted element is secret too: a table nested in a secret cell.
        self._covered: set[int] = self._covered_by(self.replacements)
        self._index: _DocIndex | None = None

    def grid(self, table: Element) -> list[TableGridRow[Element]]:
        grid = self._grids.get(id(table))
        if grid is None:
            grid, cut = layout_table_bounded(_table_rows(table, keep_empty=True), _span, MAX_GRID_CELLS)
            self._grids[id(table)] = grid
            if cut:
                reach = max((cell.end for cell in grid[-1].cells), default=table.start) if grid else table.start
                self._cut_tables[id(table)] = (table, reach)
            self.truncated = self.truncated or cut or any(row.clamped for row in grid)
        return grid

    def _unread_tail(self) -> frozenset[int]:
        """Ids of the elements of a cut table that start after its last laid-out row."""
        tail: set[int] = set()
        for table, reach in self._cut_tables.values():
            tail.update(id(el) for el in table.iter_elements() if el.start >= reach)
        return frozenset(tail)

    def is_truncated(self) -> bool:
        """The parse was cut: the document or a grid hit its bound, a span was clamped, or an element's
        text was read only to the text bound."""
        return self.truncated or _cut(self.root)

    def _plain_text(self, cell: Element) -> str:
        text = self._plain.get(id(cell))
        if text is None:
            text = self._plain[id(cell)] = cell.text()
            if cell.text_cut:
                # The words beyond the text bound are unread, so the cell may name a secret: it counts
                # as doing so (and its values as secret) rather than as an innocent long text.
                text = f"{_UNREAD_TEXT_MARKER} {text}"
                self._plain[id(cell)] = text
        return text

    @staticmethod
    def _scope_of(cells: Iterable[Element]) -> frozenset[int]:
        """Ids of the cells and of everything inside them. A cell nested in an earlier one is already
        covered, so each element is visited once however deep the nesting."""
        scope: set[int] = set()
        reach = -1
        for cell in sorted(cells, key=lambda element: element.start):
            if cell.start < reach:
                continue
            reach = cell.end
            scope.update(id(element) for element in (cell, *cell.iter_elements()))
        return frozenset(scope)

    def _secret_cells(self) -> list[tuple[Element, str]]:
        hits: list[tuple[Element, str]] = []
        for table in self.tables:
            cells, labels = govern_table(self.grid(table), self._plain_text, is_header_cell=_is_header_cell)
            hits += cells
            self._label_cells += labels
        return hits

    def _secret_replacements(self, secret_cells: list[tuple[Element, str]]) -> dict[int, str]:
        replacements: dict[int, str] = {}
        for cell, replacement in secret_cells:
            if replacement != REDACTED or self._plain_text(cell):
                replacements.setdefault(id(cell), replacement)
        # A secret control's own text (option labels, textarea content, button caption) shows up in
        # any enclosing cell's text as well.
        for tag in ("select", "textarea", "button"):
            for el in self.root.find_all(tag):
                if (is_sensitive_name(_control_name(el)) or id(el) in self.secret_scope) and el.text():
                    replacements.setdefault(id(el), REDACTED)
        return replacements

    def _covered_by(self, replacements: dict[int, str]) -> set[int]:
        covered: set[int] = set()
        if not replacements:
            return covered
        for el in self.root.iter_elements():
            if id(el) in replacements and id(el) not in covered:
                covered.update(id(inner) for inner in el.iter_elements())
        return covered

    def text(self, el: Element) -> str:
        """``el``'s text with every secret inside it (and ``el`` itself) redacted."""
        if not self.replacements:
            return el.text()
        if id(el) in self._covered:
            return REDACTED if el.text() else ""
        return self.replacements.get(id(el)) or el.text(self.replacements)

    def label_text(self, el: Element) -> str:
        """Like ``text`` for a label or header cell: any secret the cell holds is redacted, and a cell inside
        a redacted element reads as redacted."""
        if not self.replacements:
            return el.text()
        if id(el) in self._covered:
            return REDACTED if el.text() else ""
        return self.replacements.get(id(el)) or el.text(self.replacements)

    def covered(self, el: Element) -> bool:
        """True when ``el`` sits inside an element redacted as a whole (a table nested in a secret cell):
        what is derived from it, labels and headers included, is not read."""
        return id(el) in self._covered

    @property
    def index(self) -> _DocIndex:
        if self._index is None:
            self._index = _DocIndex(self.root)
        return self._index


class _DocIndex:
    """Document positions of tables, ``<h2>`` starts, ``</form>`` ends and form controls, sorted, so "the
    first table after this heading" and "does this cell hold a control" are binary searches, not walks."""

    def __init__(self, root: Element) -> None:
        self.tables = root.find_all("table")
        self.table_starts = [el.start for el in self.tables]
        self.h2_starts = [el.start for el in root.find_all("h2")]
        self.form_ends = sorted(el.end for el in root.find_all("form"))
        self.control_starts = sorted(el.start for tag in CONTROL_TAGS for el in root.find_all(tag))

    def holds_control(self, element: Element) -> bool:
        """True when a form control starts inside ``element`` (document positions, no subtree walk)."""
        position = bisect_right(self.control_starts, element.start)
        return position < len(self.control_starts) and self.control_starts[position] < element.end

    def first_table_after(self, start: int, *, stop_at_h2: bool) -> Element | None:
        """First table after document position ``start``; optionally bounded by the next ``<h2>`` or a ``</form>``."""
        position = bisect_right(self.table_starts, start)
        if position == len(self.tables):
            return None
        table = self.tables[position]
        if stop_at_h2:
            for bounds in (self.h2_starts, self.form_ends):
                nearest = bisect_right(bounds, start)
                if nearest < len(bounds) and bounds[nearest] < table.start:
                    return None
        return table


def _table_grid(table: Element, doc: _Doc) -> list[tuple[TableGridRow[Element], list[str]]]:
    """Laid-out rows of ``table`` that have cells of their own, with the (redacted) text of each column."""
    return [(row, [doc.text(cell) for cell in row.columns]) for row in doc.grid(table) if row.cells]


def _table_texts(table: Element, doc: _Doc | None = None) -> list[list[str]]:
    return [[doc.text(cell) if doc else cell.text() for cell in row] for row in _table_rows(table)]


def extract_tables(html: str) -> list[list[list[str]]]:
    """Every table as rows of cell texts (the TS ``extractTables`` shape)."""
    return [_table_texts(table) for table in parse_document(html).find_all("table")]


def _first_text(root: Element, tag: str, doc: _Doc | None = None) -> str:
    found = root.find_all(tag)
    if not found:
        return ""
    return doc.text(found[0]) if doc else found[0].text()


def _control_name(el: Element) -> str:
    return el.attr("name") or el.attr("id") or ""


def _input_type(el: Element) -> str:
    """HTML input types are ASCII case-insensitive; downstream code compares lowercase names."""
    return (el.attr("type") or "text").lower()


def _is_button_input(el: Element) -> bool:
    return _input_type(el).lower() in BUTTON_INPUT_TYPES


def _flag(el: Element, name: str) -> bool | None:
    return True if el.has(name) else None


def _dedupe_records(records: list[Record]) -> list[Record]:
    seen: set[str] = set()
    output: list[Record] = []
    for record in records:
        key = json.dumps(record, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


# --- public parsers -------------------------------------------------------------------------------


def parse_sitemap(html: str) -> list[SitemapEntry]:
    """``<a href=".../cgi-bin/<page>.ha">label</a>`` links, de-duplicated and sorted by (page, label).
    A link inside a secret cell is not listed."""
    return _sitemap_entries(_Doc(parse_document(html), False))


def _sitemap_entries(doc: _Doc) -> list[SitemapEntry]:
    entries: list[SitemapEntry] = []
    seen: set[tuple[str, str]] = set()
    for anchor in doc.root.find_all("a"):
        href = anchor.attr("href") or ""
        match = _SITEMAP_HREF.fullmatch(href)
        if not match or (not doc.include_secrets and id(anchor) in doc.secret_scope):
            continue
        page = match.group(1)
        label = doc.text(anchor)
        if not page or not label or (page, label) in seen:
            continue
        seen.add((page, label))
        entries.append(SitemapEntry(page=page, label=label, href=href))
    return sorted(entries, key=lambda entry: (entry.page, entry.label))


def parse_page(page: str, html: str, include_secrets: bool = False) -> ParsedPage:
    """Parse one router page into the shared ``ParsedPage`` model, redacting secrets unless asked not to."""
    root = parse_document(html)
    doc = _Doc(root, include_secrets)
    values: Record = {}

    value_entries = _parse_value_entries(doc)
    for entry in value_entries:
        if entry.value:
            values[entry.label] = entry.value

    generic_tables = _parse_generic_tables(doc)

    for h1 in root.find_all("h1"):
        parts = _CURRENTLY.split(doc.text(h1))
        if len(parts) == 2 and parts[0] and parts[1]:
            values[parts[0].strip()] = redact_value(parts[0], parts[1].strip(), include_secrets)

    for index, description in enumerate(_parse_description_blocks(doc)):
        key = "Description" if index == 0 else f"Description {index + 1}"
        values[key] = redact_value("Description", description, include_secrets)

    fields = _parse_fields(doc)
    selects = _parse_selects(doc)
    textareas = _parse_textareas(doc)
    buttons = _parse_buttons(doc)
    forms = _parse_forms(doc)
    links = _parse_links(doc)

    for control in fields:
        if control.type in ("radio", "checkbox") and not control.checked:
            continue
        if control.value and control.name not in ("nonce", "hashpassword"):
            values[f"Field {control.name}"] = control.value
    for select in selects:
        if select.value:
            values[f"Field {select.name}"] = select.value
    for textarea in textareas:
        display = normalize_whitespace(textarea.value)
        if display:
            values[f"Field {textarea.name}"] = display

    if page == "sitemap":
        parsed_tables = [{"Page": e.page, "Label": e.label, "Href": e.href} for e in _sitemap_entries(doc)]
    elif page == "speed":
        parsed_tables = _parse_speed_tables(doc)
    elif page == "diag":
        parsed_tables = _parse_diagnostic_tables(doc)
    else:
        parsed_tables = _dedupe_records(generic_tables)
    if page in ("etherlan", "fiberstat", "voice", "voiceconfig", "voicestat"):
        parsed_tables = []
    page_tables = _parse_page_specific_tables(page, doc, fields, selects)

    return ParsedPage(
        page=page,
        title=_first_text(root, "title", doc),
        heading=_heading(doc),
        values=values,
        value_entries=value_entries,
        tables=_dedupe_records([*parsed_tables, *page_tables]),
        fields=fields,
        selects=selects,
        textareas=textareas,
        buttons=buttons,
        forms=forms,
        links=links,
        truncated=True if doc.is_truncated() else None,
    )


def _device_tables(html: str) -> list[list[list[str]]]:
    return [
        table for table in extract_tables(html) if any("mac address" in cell.lower() for row in table for cell in row)
    ]


def has_device_table(html: str) -> bool:
    """True when the document carries a device table (a header or key naming ``MAC Address``), with or
    without rows: an empty device list still has one, a busy or unrelated page does not."""
    return bool(_device_tables(html))


def parse_devices(html: str) -> list[Device]:
    """Device list rows from either the key/value table shape or the wide table shape."""
    devices: list[Device] = []
    for table in _device_tables(html):
        if any(row[0] == "MAC Address" and len(row) == 2 for row in table):
            devices.extend(_devices_from_key_value_table(table))
            continue
        for row in table[1:]:
            if len(row) < 4:
                continue
            status, name_ip = row[0], row[1]
            ip, name = split_ip_and_name(name_ip)
            # Without an "address / name" pair the column is the name and the address is the next
            # column (a lone address only reads as one under the combined "IPv4 Address / Name" header).
            if not ip or (not name and "/" not in name_ip and table[0][1:2] != ["IPv4 Address / Name"]):
                ip, name = row[2], name_ip
            devices.append(
                Device(
                    status=status,
                    name=name,
                    ip=ip or "Unknown",
                    mac=row[3],
                    connection=row[4] if len(row) > 4 else "Unknown",
                )
            )
    return devices


def _devices_from_key_value_table(table: list[list[str]]) -> list[Device]:
    devices: list[Device] = []
    current: Record = {}

    def flush() -> None:
        if not current.get("MAC Address"):
            return
        name_ip = current.get("IPv4 Address / Name", current.get("Name", ""))
        ip, name = split_ip_and_name(name_ip)
        if not ip:
            name = current.get("Name", name_ip)
        elif not name and "/" not in name_ip:
            name = current.get("Name", "")
        devices.append(
            Device(
                status=current.get("Status", ""),
                name=name,
                ip=ip,
                mac=current.get("MAC Address", ""),
                connection=current.get("Connection Type", ""),
                connection_speed=current.get("Connection Speed"),
                allocation=current.get("Allocation"),
                last_activity=current.get("Last Activity"),
                mesh_client=current.get("Mesh Client"),
                ipv6=current.get("IPv6 Address"),
                device_type=current.get("Type"),
                valid_lifetime=current.get("Valid Lifetime"),
                preferred_lifetime=current.get("Preferred Lifetime"),
            )
        )

    for row in table:
        if len(row) < 2 or not row[0]:
            continue
        key, value = row[0], row[1]
        if key == "MAC Address":
            flush()
            current = {}
        current[key] = value
    flush()
    return devices


_LOG_HEADER_WORDS = ("time", "source", "destination", "protocol", "reason")


def parse_logs(html: str) -> list[LogEntry] | None:
    """Rows of the first table that has a log header: a first row of at least six columns naming at
    least three of time, source, destination, protocol and reason (header row skipped, short rows
    ignored). None when no table has one - the page is not the log (a busy or error document), which
    is different from a log with no entries (an empty list). A log page the parser cut at a bound
    raises ``TruncatedPageError`` rather than return part of its entries."""
    root = parse_document(html)
    for table in (_table_texts(table) for table in root.find_all("table")):
        header = [cell.lower() for cell in table[0]] if table else []
        if len(header) >= 6 and sum(any(word in cell for cell in header) for word in _LOG_HEADER_WORDS) >= 3:
            if _cut(root):
                raise TruncatedPageError("the log page was cut by the parser's bounds; its entries are not complete")
            return [
                LogEntry(id=row[0], time=row[1], source=row[2], destination=row[3], protocol=row[4], reason=row[5])
                for row in table[1:]
                if len(row) >= 6
            ]
    return None


def _cut(root: Element) -> bool:
    """True when a bound cut the document or an element's text (see ``_Doc.is_truncated``)."""
    return root.truncated or any(el.text_cut for els in root.by_tag.values() for el in els)


def looks_like_login(html: str) -> bool:
    """True when the router answered with its login page instead of the requested page.

    The login page is recognised by its title or top heading ("Login", or one carrying "Access
    Code Required") or by the login form itself: a form posting to login.ha that holds both the
    ``nonce`` input and the password field. The same words in body text or help prose on an
    ordinary page do not count.
    """
    root = parse_document(html)
    for tag in ("title", "h1"):
        text = _first_text(root, tag)
        if _LOGIN_TITLE.match(text) or _ACCESS_CODE_REQUIRED.search(text):
            return True
    return any(_is_login_form(form) for form in root.find_all("form"))


def _is_login_form(form: Element) -> bool:
    if not _LOGIN_ACTION.search(form.attr("action") or ""):
        return False
    controls = [el for el in form.iter_elements() if el.tag == "input"]
    has_nonce = any(_control_name(el) == "nonce" for el in controls)
    has_password = any(
        _input_type(el) == "password" or (el.attr("name") or el.attr("id") or "").lower() == "password"
        for el in controls
    )
    return has_nonce and has_password


# --- value entries, descriptions, links ---------------------------------------------------------


def _heading_text(heading: Element, doc: _Doc) -> str:
    """A heading's own words, with secrets redacted. A heading left unclosed owns the markup that follows
    it, so tables and further headings nested in it are not part of its text."""
    nested = {
        id(el): "" for el in heading.iter_elements(stop=_SECTION_BREAKS) if el.tag in _SECTION_BREAKS
    }
    if not nested:
        return doc.text(heading)
    if id(heading) in doc._covered:
        return REDACTED
    return heading.text({**doc.replacements, **nested})


def _table_sections(doc: _Doc) -> dict[int, str]:
    """``id(table) -> text of the last h1/h2/h3 that starts before it``, in one document-order pass."""
    sections: dict[int, str] = {}
    heading: Element | None = None
    heading_text = ""
    breaks = (doc.root.find_all(tag) for tag in ("h1", "h2", "h3", "table"))
    for el in heapq.merge(*breaks, key=lambda element: element.start):
        if el.tag in _HEADING_TAGS:
            heading, heading_text = el, ""
        else:
            if heading is not None and not heading_text:
                heading_text = _heading_text(heading, doc)
            sections[id(el)] = heading_text
    return sections


def split_default_label(label: str) -> tuple[str, str | None]:
    """``"Password Default: abc"`` -> ``("Password Default", "abc")``; a label without a
    ``Default:`` value comes back unchanged with ``None``. Labels are whitespace-normalised."""
    match = _DEFAULT_LABEL.match(label)
    if not match or not match.group(1) or not match.group(2):
        return label, None
    return normalize_whitespace(match.group(1)), normalize_whitespace(match.group(2))


def _split_default_value(label: str, include_secrets: bool) -> tuple[str, str | None]:
    label, default = split_default_label(label)
    return label, None if default is None else redact_value(label, default, include_secrets)


def _parse_value_entries(doc: _Doc) -> list[ParsedValueEntry]:
    include_secrets = doc.include_secrets
    entries: list[ParsedValueEntry] = []
    sections = _table_sections(doc)
    for table in doc.tables:
        section = sections[id(table)]
        for row, _texts in _table_grid(table, doc):
            cells = row.cells
            if len(cells) != 2 or doc.index.holds_control(cells[1]):
                continue
            # A label cell spanning columns is a column header, and a row an earlier rowspan cell
            # reaches into continues that cell's record: neither is a label/value pair.
            if row.continued or _span(cells[0], "colspan") > 1:
                continue
            if doc.covered(cells[0]):
                continue  # inside a redacted element: its label is as secret as its value
            raw_label = re.sub(r":$", "", doc.label_text(cells[0]))
            if not raw_label:
                continue
            label, default_value = _split_default_value(raw_label, include_secrets)
            value = redact_value(label, doc.text(cells[1]), include_secrets)
            entries.append(ParsedValueEntry(section=section, label=label, value=value, default_value=default_value))
    return entries


def _parse_description_blocks(doc: _Doc) -> list[str]:
    texts = (doc.text(div) for div in doc.root.find_all("div") if _DESC_CLASS.search(div.attr("class") or ""))
    return _unique([text for text in texts if text])


def _heading(doc: _Doc) -> str:
    """The first ``<h1>``'s text; a ``<label> Currently <value>`` heading keeps a secret label's value redacted."""
    text = _first_text(doc.root, "h1", doc)
    parts = _CURRENTLY.split(text)
    if len(parts) == 2 and parts[0] and parts[1]:
        value = redact_value(parts[0], parts[1].strip(), doc.include_secrets)
        if value != parts[1].strip():
            return f"{parts[0].strip()} Currently {value}"
    return text


# A query/parameter value whose name is a secret (`?password=abc&page=2`).
_HREF_PARAMETER = re.compile(r"(?<=[?&;])([^=&;#\s]+)=([^&;#\s]*)")


def redact_href(href: str, include_secrets: bool = False) -> str:
    """``href`` with the value of every secret-named query parameter redacted; a name is judged as
    the server decodes it (``%70assword`` is ``password``)."""
    if include_secrets:
        return href
    return _HREF_PARAMETER.sub(
        lambda m: (
            f"{m.group(1)}={REDACTED}" if m.group(2) and is_sensitive_name(unquote(m.group(1))) else m.group(0)
        ),
        href,
    )


def href_secret_values(href: str) -> list[str]:
    """The values of the secret-named query parameters of ``href`` that are not already redacted."""
    return [
        m.group(2)
        for m in _HREF_PARAMETER.finditer(href)
        if m.group(2) and m.group(2) != REDACTED and is_sensitive_name(unquote(m.group(1)))
    ]


def _scoped_href(href: str, el: Element, doc: _Doc) -> str:
    """A link or form target: replaced wholesale inside a secret scope, else its secret parameters redacted."""
    if href and not doc.include_secrets and id(el) in doc.secret_scope:
        return REDACTED
    return redact_href(href, doc.include_secrets)


def _anchor_title(anchor: Element, doc: _Doc) -> str:
    """An anchor's ``title`` as its label when it has no text: redacted when the anchor sits in
    (or is) an element that holds a secret, exactly as its text would be."""
    title = normalize_whitespace(anchor.attr("title") or "")
    if title and not doc.include_secrets and id(anchor) in doc.secret_scope:
        return REDACTED
    return title


def _parse_links(doc: _Doc) -> list[ParsedLink]:
    links: list[ParsedLink] = []
    seen: set[tuple[str, str]] = set()
    for anchor in doc.root.find_all("a"):
        href = _scoped_href(anchor.attr("href") or "", anchor, doc)
        if not href:
            continue
        label = doc.text(anchor) or _anchor_title(anchor, doc) or href
        match = _CGI_PAGE.search(href)
        page = match.group(1) if match else None
        kind = "router-page" if page else ("external" if _HTTP_URL.match(href) else "other")
        if (href, label) in seen:
            continue
        seen.add((href, label))
        links.append(ParsedLink(label=label, href=href, kind=kind, page=page))
    return links


# --- controls -------------------------------------------------------------------------------------


def _parse_fields(doc: _Doc) -> list[ParsedField]:
    include_secrets = doc.include_secrets
    fields: list[ParsedField] = []
    for el in doc.root.find_all("input"):
        name = _control_name(el)
        if not name or _is_button_input(el):
            continue
        input_type = _input_type(el)
        value = el.attr("value")
        if value is None:
            # Checkboxes/radios use HTML's default/on mode; an explicit empty value stays empty.
            value = "on" if input_type.lower() in ("checkbox", "radio") else ""
        # A password-type input is secret whatever it is called.
        sensitive = is_sensitive_name(name) or input_type.lower() == "password" or id(el) in doc.secret_scope
        fields.append(
            ParsedField(
                name=name,
                type=input_type,
                value=value if include_secrets or not sensitive or not value else REDACTED,
                checked=el.has("checked"),
                sensitive=sensitive,
                disabled=_flag(el, "disabled"),
                read_only=_flag(el, "readonly"),
                _value_omitted=not el.has("value"),
            )
        )
    return fields


def _parse_selects(doc: _Doc) -> list[ParsedSelect]:
    include_secrets = doc.include_secrets
    selects: list[ParsedSelect] = []
    for el in doc.root.find_all("select"):
        name = _control_name(el)
        if not name:
            continue
        secret = (name,) if id(el) in doc.secret_scope else ()
        options: list[SelectOption] = []
        for option in el.find_all("option"):
            label = option.text()
            # As in HTML, only a missing value attribute falls back to the text; value="" stays empty.
            value = option.attr("value")
            options.append(
                SelectOption(
                    value=label if value is None else value,
                    label=label,
                    selected=option.has("selected"),
                    disabled=option.has("disabled"),
                )
            )
        selected = next((o for o in options if o.selected), options[0] if options else None)
        selects.append(
            ParsedSelect(
                name=name,
                value=redact_value(name, selected.value if selected else "", include_secrets, secret),
                options=[redact_value(name, o.value, include_secrets, secret) for o in options],
                sensitive=is_sensitive_name(name) or bool(secret),
                option_details=[
                    SelectOption(
                        value=redact_value(name, o.value, include_secrets, secret),
                        label=redact_value(name, o.label, include_secrets, secret),
                        selected=o.selected,
                        disabled=o.disabled,
                    )
                    for o in options
                ],
                disabled=_flag(el, "disabled"),
            )
        )
    return selects


def _parse_textareas(doc: _Doc) -> list[ParsedTextarea]:
    include_secrets = doc.include_secrets
    textareas: list[ParsedTextarea] = []
    for el in doc.root.find_all("textarea"):
        name = _control_name(el)
        if not name:
            continue
        secret = (name,) if id(el) in doc.secret_scope else ()
        textareas.append(
            ParsedTextarea(
                name=name,
                value=redact_value(name, el.raw_text(), include_secrets, secret),
                sensitive=is_sensitive_name(name) or bool(secret),
                disabled=_flag(el, "disabled"),
                read_only=_flag(el, "readonly"),
            )
        )
    return textareas


def _parse_buttons(doc: _Doc) -> list[ParsedButton]:
    """Button-type ``<input>`` controls first (document order), then ``<button>`` elements — as the TS CLI did.

    A button inside a secret scope (a secret-labelled cell, the unread part of a cut table) is
    ``sensitive``: its value, caption and any name taken from them are redacted."""
    root, include_secrets = doc.root, doc.include_secrets
    buttons: list[ParsedButton] = []
    for el in root.find_all("input"):
        if not _is_button_input(el):
            continue
        scoped = id(el) in doc.secret_scope
        button_type = _input_type(el).lower()
        value = el.attr("value") or ""
        name = _control_name(el) or (REDACTED if scoped and not include_secrets and value else value or button_type)
        secret = (name,) if scoped else ()
        buttons.append(
            ParsedButton(
                name=name,
                type=button_type,
                value=redact_value(name, value, include_secrets, secret),
                label=redact_value(name, normalize_whitespace(value) or name, include_secrets, secret),
                sensitive=is_sensitive_name(name) or scoped,
                disabled=_flag(el, "disabled"),
            )
        )
    for el in root.find_all("button"):
        scoped = id(el) in doc.secret_scope
        button_type = el.attr("type") or "submit"
        label = doc.text(el)
        value_attr = el.attr("value")
        derived = value_attr or label
        name = _control_name(el) or (REDACTED if scoped and not include_secrets and derived else derived or button_type)
        secret = (name,) if scoped else ()
        buttons.append(
            ParsedButton(
                name=name,
                type=button_type,
                value=redact_value(name, label if value_attr is None else value_attr, include_secrets, secret),
                label=redact_value(name, label or value_attr or name, include_secrets, secret),
                sensitive=is_sensitive_name(name) or scoped,
                disabled=_flag(el, "disabled"),
            )
        )
    return buttons


def _parse_forms(doc: _Doc) -> list[ParsedForm]:
    forms: list[ParsedForm] = []
    for form in doc.root.find_all("form"):
        inputs = form.find_all("input")
        forms.append(
            ParsedForm(
                method=(form.attr("method") or "GET").upper(),
                action=_scoped_href(form.attr("action") or "", form, doc),
                field_names=_unique([_control_name(i) for i in inputs if not _is_button_input(i) and _control_name(i)]),
                select_names=_unique([_control_name(s) for s in form.find_all("select") if _control_name(s)]),
                textarea_names=_unique([_control_name(t) for t in form.find_all("textarea") if _control_name(t)]),
                button_names=_unique(
                    [_control_name(i) for i in inputs if _is_button_input(i) and _control_name(i)]
                    + [_control_name(b) for b in form.find_all("button") if _control_name(b)]
                ),
            )
        )
    return forms


# --- tables ---------------------------------------------------------------------------------------


def _parse_generic_tables(doc: _Doc) -> list[Record]:
    """Tables with more than two header columns become one record per same-width row (header -> cell).

    A colspan header names each column it covers ("Name", "Name 2", ...); a rowspan cell's value is
    carried into the rows it covers; a data row with a colspan cell does not map onto the columns
    and is skipped, as is a further row made only of ``<th>`` cells (a second header row).
    """
    include_secrets = doc.include_secrets
    records: list[Record] = []
    for table in doc.tables:
        rows = _table_grid(table, doc)
        if not rows or len(rows[0][1]) <= 2 or doc.covered(table):
            continue
        headers = column_headers([doc.label_text(cell) for cell in rows[0][0].columns], rows[0][0].span_copies)
        for row, texts in rows[1:]:
            if row.span_copies or len(texts) != len(headers) or all(cell.tag == "th" for cell in row.cells):
                continue
            record: Record = {}
            for index, header in enumerate(headers):
                key = header or ("Metric" if index == 0 else "")
                if key:
                    record[key] = redact_value(key, texts[index], include_secrets)
            if record:
                records.append(record)
    return records


def _parse_speed_tables(doc: _Doc) -> list[Record]:
    return [
        {"Time": row[0], "Direction": row[1], "Mbps": row[2], "Server": row[3], "Latency ms": row[4], "Result": row[5]}
        for table in doc.tables
        for row in _table_texts(table, doc)
        if len(row) >= 6
    ]


def _parse_diagnostic_tables(doc: _Doc) -> list[Record]:
    return [
        {"Test": row[0], "Status": row[1]}
        for table in doc.tables
        for row in _table_texts(table, doc)
        if len(row) >= 2 and row[0] in ("Ethernet", "Authentication", "IP", "DNS")
    ]


def _parse_page_specific_tables(
    page: str, doc: _Doc, fields: list[ParsedField], selects: list[ParsedSelect]
) -> list[Record]:
    if page == "wconfig_unified":
        return _parse_wifi_channel_tables(doc)
    # The wconfig, etherlan and wmacauth summaries are built from the page's form controls. A
    # response carrying no form control at all (an empty body, a busy or error page) has nothing
    # to summarise, and static labels with empty values must not count as observed data. Once the
    # form is there the summary keeps its full fixed shape (the dump format the TS CLI wrote).
    if page in ("wconfig", "etherlan", "wmacauth") and not _has_form_controls(fields, selects):
        return []
    if page == "wconfig":
        return _parse_advanced_wifi_tables(fields, selects)
    if page == "etherlan":
        return _parse_ethernet_port_tables(selects)
    if page == "wmacauth":
        return _parse_wifi_mac_filter_tables(selects)
    if page == "fiberstat":
        return _parse_fiber_threshold_tables(doc)
    if page in ("voice", "voiceconfig"):
        return _parse_voice_status_tables(doc)
    if page == "voicestat":
        return _parse_voice_statistics_tables(doc)
    return []


def _has_form_controls(fields: list[ParsedField], selects: list[ParsedSelect]) -> bool:
    """True when the page carries a data control other than the session nonce/hash inputs."""
    return bool(selects) or any(f.name not in ("nonce", "hashpassword") for f in fields)


def _parse_wifi_channel_tables(doc: _Doc) -> list[Record]:
    channels: list[Record] = []
    for h2 in doc.root.find_all("h2"):
        match = _BAND_HEADING.match(doc.text(h2))
        if not match:
            continue
        band = normalize_whitespace(match.group(1))
        table = doc.index.first_table_after(h2.end, stop_at_h2=True)
        rows = _table_texts(table, doc) if table else []
        row = next((r for r in rows if r[0] == "Current Channel"), None)
        if row is None:
            continue
        current = row[1] if len(row) > 1 else ""
        mode = row[2] if len(row) > 2 else ""
        parsed = list(_CHANNEL_WIDTH.finditer(current))
        if not parsed:
            channels.append({"Radio": band, "Current Channel": current, "Channel Width": "", "Mode": mode})
            continue
        for entry in parsed:
            channel = entry.group(1)
            channels.append(
                {
                    "Radio": _radio_name(band, channel, len(parsed)),
                    "Current Channel": channel,
                    "Channel Width": normalize_whitespace(entry.group(2)),
                    "Mode": mode,
                }
            )
    return channels


def _radio_name(band: str, channel: str, count: int) -> str:
    if band.lower().startswith("2.4") or count == 1:
        return band
    return "5 GHz low-band" if int(channel) < 100 else "5 GHz high-band"


def _parse_advanced_wifi_tables(fields: list[ParsedField], selects: list[ParsedSelect]) -> list[Record]:
    def field_value(name: str) -> str:
        return next((f.value for f in fields if f.name == name), "")

    def select_value(name: str) -> str:
        return next((s.value for s in selects if s.name == name), "")

    return [
        {
            "Section": "Radio",
            "Radio": "2.4 GHz",
            "Enabled": select_value("wl80211on"),
            "Standard": select_value("standard"),
            "Bandwidth": select_value("bandwidth"),
            "Channel": select_value("channelplusauto"),
            "Power level (%)": field_value("power"),
        },
        {
            "Section": "Radio",
            "Radio": "5 GHz (combined configuration)",
            "Enabled": select_value("wl80211on_5"),
            "Standard": select_value("standard_5"),
            "Bandwidth": select_value("bandwidth_5"),
            "Channel": "",
            "Power level (%)": field_value("power_5"),
        },
        {
            "Section": "SSID",
            "Radio": "2.4 GHz",
            "Network": "Home",
            "Enabled": select_value("ussidenable"),
            "SSID": field_value("ssidname11"),
            "Hidden": select_value("hide"),
            "Security": select_value("security11"),
            "WPA version": select_value("wpaversion"),
            "WPS": select_value("wps"),
            "Maximum clients": field_value("maxclients"),
        },
        {
            "Section": "SSID",
            "Radio": "2.4 GHz",
            "Network": "Guest",
            "Enabled": select_value("gssidenable"),
            "SSID": field_value("ssidname12"),
            "Hidden": select_value("hide2"),
            "Security": select_value("security12"),
            "WPA version": select_value("wpaversion2"),
            "WPS": "",
            "Maximum clients": field_value("maxclients2"),
        },
        {
            "Section": "SSID",
            "Radio": "5 GHz (combined configuration)",
            "Network": "Home",
            "Enabled": select_value("wl80211on_5"),
            "SSID": field_value("ssidname21"),
            "Hidden": select_value("hide_5"),
            "Security": select_value("security21"),
            "WPA version": select_value("wpaversion_5"),
            "WPS": select_value("wps_5"),
            "Maximum clients": field_value("maxclients_5"),
        },
    ]


def _option_labels(select: ParsedSelect | None) -> list[str]:
    if select is None:
        return []
    if select.option_details is not None:
        return [o.label for o in select.option_details]
    return list(select.options)


def _parse_ethernet_port_tables(selects: list[ParsedSelect]) -> list[Record]:
    records: list[Record] = []
    for port in (1, 2, 3, 4):
        media = next((s for s in selects if s.name == f"enet{port}_port{port}_media"), None)
        mdix = next((s for s in selects if s.name == f"enet{port}_port{port}_mdix"), None)
        records.append(
            {
                "Port": str(port),
                "Configured media": media.value if media else "",
                "Configured MDI-X": mdix.value if mdix else "",
                "Supported media modes": ", ".join(_option_labels(media)),
                "Supported MDI-X modes": ", ".join(_option_labels(mdix)),
            }
        )
    return records


def _parse_wifi_mac_filter_tables(selects: list[ParsedSelect]) -> list[Record]:
    definitions = (
        ("2.4 GHz", "Home", "wmacr1user"),
        ("2.4 GHz", "Guest", "wmacr1guest"),
        ("5 GHz", "Home", "wmacr2user"),
    )
    return [
        {"Radio": radio, "Network": network, "Filtering": next((s.value for s in selects if s.name == name), "")}
        for radio, network, name in definitions
    ]


def _parse_fiber_threshold_tables(doc: _Doc) -> list[Record]:
    records: list[Record] = []
    for h1 in doc.root.find_all("h1"):
        heading = doc.text(h1)
        parts = _FIBER_CURRENTLY.split(heading, maxsplit=1)
        if len(parts) != 2:
            continue
        measurement, current = parts
        table = doc.index.first_table_after(h1.end, stop_at_h2=False)
        rows = _table_texts(table, doc) if table else []
        for row in rows[1:]:
            if len(row) != 3:
                continue
            records.append(
                {
                    "Measurement": normalize_whitespace(measurement),
                    "Current": normalize_whitespace(current),
                    "State": row[0],
                    "Low": row[1],
                    "High": row[2],
                }
            )
    return records


def _parse_voice_statistics_tables(doc: _Doc) -> list[Record]:
    include_secrets = doc.include_secrets
    tables = [_table_texts(t, doc) for t in doc.tables]
    records: list[Record] = []

    for index, line in enumerate(("Line 1", "Line 2")):
        table = tables[index] if index < len(tables) else []
        for row in table[2:]:
            if len(row) != 5:
                continue
            records.append(
                {
                    "Section": "Call statistics",
                    "Line": line,
                    "Metric": row[0],
                    "Last call incoming": row[1],
                    "Last call outgoing": row[2],
                    "Cumulative incoming": row[3],
                    "Cumulative outgoing": row[4],
                }
            )

    summary = tables[2] if len(tables) > 2 else []
    for row in summary[2:]:
        if len(row) != 5:
            continue
        metric = row[0]
        records.append(
            {
                "Section": "Call summary",
                "Line": "Both",
                "Metric": metric,
                "Line 1 current": redact_value(metric, row[1], include_secrets),
                "Line 1 last": redact_value(metric, row[2], include_secrets),
                "Line 2 current": redact_value(metric, row[3], include_secrets),
                "Line 2 last": redact_value(metric, row[4], include_secrets),
            }
        )

    cumulative = tables[3] if len(tables) > 3 else []
    for row in cumulative[1:]:
        if len(row) != 3:
            continue
        records.append(
            {
                "Section": "Cumulative since last reset",
                "Line": "Both",
                "Metric": row[0],
                "Line 1": row[1],
                "Line 2": row[2],
            }
        )
    return records


def _parse_voice_status_tables(doc: _Doc) -> list[Record]:
    include_secrets = doc.include_secrets
    records: list[Record] = []
    for table in doc.tables:
        for row in _table_texts(table, doc)[1:]:
            if len(row) != 3 or not row[0]:
                continue
            metric = row[0]
            records.append(
                {
                    "Metric": metric,
                    "Line 1": redact_value(metric, row[1], include_secrets),
                    "Line 2": redact_value(metric, row[2], include_secrets),
                }
            )
    return records
