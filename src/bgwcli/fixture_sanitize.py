"""Sanitize captured router HTML/JSON before it is written as a test fixture.

Redacts MAC addresses, IPv4/IPv6 addresses, secret-bearing form values (nonce, hashes,
passwords, keys, SSIDs, WPS PINs, serials, hostnames), identity table cells, table values under a
label or header the parser treats as secret, and device labels. Form controls are located by
parsing the markup (unquoted attributes, id-only controls, password-type inputs named or not,
textarea content, select options and values containing quotes are all covered), then the value
attribute, textarea text, or every option value and label of a sensitive select is rewritten in
place. The text of a control-bearing cell under a secret label is redacted as well as its control
values, and a secret query value in a form action or link target is redacted (names judged
percent-decoded).
`contains_sensitive_fixture_value` (serialized text) and `sensitive_control_residue` (an
include_secrets parse of the sanitized HTML) are the fail-closed residue checks, and
`fixture_secret_values` lists the secrets found in the original so the capture can search the
sanitized page for them literally.

The locator is bounded like the parser (elements, nesting, a table's grid, one cell's text, spans):
a page past a bound raises ``FixtureBoundsError`` and is not captured, never an unbounded run.
"""

from __future__ import annotations

import html as html_module
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from itertools import accumulate
from typing import Any

from .errors import BgwError
from .html import normalize_whitespace
from .parser import (
    CONTROL_TAGS,
    MAX_ELEMENTS,
    MAX_GRID_CELLS,
    MAX_NESTING,
    MAX_TEXT_CHARS,
    MAX_TEXT_PARTS,
    RawTextHTMLParser,
    govern_table,
    href_secret_values,
    layout_table_bounded,
    raw_span_value,
    redact_href,
)
from .redact import is_sensitive_name
from .types import ParsedPage


class FixtureBoundsError(BgwError):
    """The page is past a bound the sanitizer shares with the parser, so it cannot be sanitized reliably."""


# Name fragments never cross a quote or tag boundary, so a match cannot run from one control's
# name into the next control's value. The text around a keyword is length-limited so a long run of
# name-like characters cannot make the match quadratic; a longer name is still caught by the
# parser-based locator, which tests the whole name.
_NAME_PAD = r"[^\"'<>]{0,100}?"
_SENSITIVE_INPUT_NAMES = (
    rf"(?:nonce|hashpassword|password|{_NAME_PAD}pass{_NAME_PAD}|{_NAME_PAD}key{_NAME_PAD}|{_NAME_PAD}ssid{_NAME_PAD}"
    rf"|{_NAME_PAD}pwd{_NAME_PAD}|{_NAME_PAD}pw(?![a-z]){_NAME_PAD}|{_NAME_PAD}psk{_NAME_PAD}"
    rf"|{_NAME_PAD}community{_NAME_PAD}|{_NAME_PAD}token{_NAME_PAD}|{_NAME_PAD}secret{_NAME_PAD}"
    rf"|wps.?pin{_NAME_PAD}|serial(?:number)?|hostname|device.?name)"
)

_SENSITIVE_CONTROL_NAME = re.compile(_SENSITIVE_INPUT_NAMES, re.IGNORECASE)
_NOT_DATA_INPUT_TYPES = frozenset({"checkbox", "radio", "submit", "button", "reset", "image"})
_BUTTON_INPUT_TYPES = _NOT_DATA_INPUT_TYPES - {"checkbox", "radio"}
# Unquoted values follow HTMLParser's attribute rule: everything up to whitespace or `>`, so a
# value containing a quote, `=` or backtick is replaced whole.
_VALUE_ATTR = re.compile(r"""(\svalue\s*=\s*)(?:"[^"]*"|'[^']*'|(?!['"])[^\s>]+)""", re.IGNORECASE)
_VALUE_ATTR_TEXT = re.compile(r"""\svalue\s*=\s*(?:"([^"]*)"|'([^']*)'|(?!['"])([^\s>]+))""", re.IGNORECASE)
# The target of a form (action) or link (href) attribute, quoted or not.
_TARGET_ATTR = re.compile(r"""(\s(?:action|href)\s*=\s*)("[^"]*"|'[^']*'|(?!['"])[^\s>]+)""", re.IGNORECASE)
# Markup tokens inside a cell: a tag (quoted attribute values may hold ">") or a text run. A comment
# reads as a tag up to its first ">", and what follows as text: redacting it too is the safe side.
_MARKUP_TOKEN = re.compile(r"""<(?:"[^"]*"|'[^']*'|[^'"<>])*>|(?P<text>[^<]+)""")
_REDACTED = "[redacted]"
# A secret shorter than this is not searched for literally in the sanitized page: it would match
# ordinary words and numbers.
_MIN_LITERAL_SECRET = 6

_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\b")
_IPV6_FULL = re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.IGNORECASE)
# An IPv6 address is at most 39 characters, so its "::" is within the first 39.
_IPV6_COMPRESSED = re.compile(r"\b(?=[0-9a-f:]{0,39}::)(?:[0-9a-f]{0,4}:){1,7}[0-9a-f]{0,4}\b", re.IGNORECASE)
_NAME_THEN_VALUE = re.compile(
    r"(name=[\"']" + _SENSITIVE_INPUT_NAMES + r"[\"'][^>]{0,1024}value=[\"'])[^\"']*([\"'])",
    re.IGNORECASE,
)
_VALUE_THEN_NAME = re.compile(
    r"(value=[\"'])[^\"']*([\"'][^>]{0,1024}name=[\"']" + _SENSITIVE_INPUT_NAMES + r"[\"'])",
    re.IGNORECASE,
)
_LABEL_DEFAULT = re.compile(
    r"((?:Network Name \(SSID\)|Guest Network Name|Password)\s{1,64}Default:\s{0,64})[^<\r\n]+",
    re.IGNORECASE,
)
_IDENTITY_CELL = re.compile(
    r"(<t[dh]\b[^>]*>\s*(?:Serial Number|Vendor SN|Phone Number|Far-End Caller Information)\s*</t[dh]>\s*"
    r"<t[dh]\b[^>]*>).*?(</t[dh]>)",
    re.IGNORECASE | re.DOTALL,
)
_IP_SLASH_NAME = re.compile(r"(\[redacted-ip\]\s*/\s*)[^<\r\n]+", re.IGNORECASE)
_SLASH_MAC = "/[redacted-mac]"
_NAME_BREAKS = frozenset("<>\r\n/")

_RESIDUE_SENSITIVE_JSON = re.compile(r'"value":"(?!\[redacted\])[^"]+"[^{}]{0,160}"sensitive":true', re.IGNORECASE)
_RESIDUE_LABEL_DEFAULT = re.compile(
    r"(?:Network Name \(SSID\)|Guest Network Name|Password)\s+Default:(?!\s*\[redacted\])\s*\S",
    re.IGNORECASE,
)
_RESIDUE_HASH = re.compile(r"\b[a-f0-9]{32}\b", re.IGNORECASE)

Edit = tuple[int, int, str]


def is_sensitive_fixture_control(name: str, control_type: str = "text") -> bool:
    """A value-bearing control (input, textarea, select) whose value must not reach a fixture.
    Checkboxes, radios and buttons carry fixed option values, not secrets."""
    kind = control_type.lower()
    if kind in _NOT_DATA_INPUT_TYPES:
        return False
    return kind == "password" or bool(_SENSITIVE_CONTROL_NAME.fullmatch(name)) or is_sensitive_name(name)


@dataclass
class _Cell:
    start: int
    spans: dict[str, int]
    end: int = -1
    has_control: bool = False
    has_table: bool = False
    header: bool = False
    # The locator that reads this cell's text from the document by position (never copied into
    # every enclosing cell), and the text once read.
    owner: Any = field(default=None, repr=False)
    _text: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = self.owner.text_between(self.start, self.end)
        return self._text


@dataclass
class _Control:
    """A control as found in the source: where its start tag sits and, for a textarea or select,
    where its content sits."""

    tag: str
    start: int
    end: int
    content_end: int = -1


@dataclass
class _Table:
    rows: list[list[_Cell]] = field(default_factory=list)
    row_open: bool = False
    cell: _Cell | None = None

    def close_cell(self, offset: int) -> None:
        if self.cell is not None:
            self.cell.end = offset
            self.cell = None


class _FixtureLocator(RawTextHTMLParser):
    """One pass over the markup collecting source spans: sensitive input start tags, sensitive
    textarea contents, the contents of sensitive selects (their option tags and labels), form and
    link targets, buttons, and the table cells grouped as the parser groups them (rows of td/th cells
    per table, nested tables separate). Text is kept once, with its position; a cell's text is the
    text between its start and end, read when asked and only to the parser's text bound."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self._source_length = len(source)
        self._line_starts = [0, *(match.end() for match in re.finditer("\n", source))]
        self.tag_spans: list[tuple[int, int]] = []
        self.text_spans: list[tuple[int, int]] = []
        self.select_spans: list[tuple[int, int]] = []
        self._textarea_content_start: int | None = None
        self._select_content_start: int | None = None
        self.password_values = False
        self.controls: list[_Control] = []
        self.buttons: list[_Control] = []
        self._open_control: dict[str, _Control] = {}
        self._stack: list[_Table] = []
        self.tables: list[_Table] = []
        self._elements = 0
        self._piece_positions: list[int] = []
        self._pieces: list[str] = []
        # Edits to form and link start tags whose target carries a secret query value.
        self.tag_edits: list[Edit] = []
        # Secrets of the original that are literal values: control values, query values.
        self.secret_values: list[str] = []
        # Source spans of the content of secret controls (textarea text, select options).
        self.secret_spans: list[tuple[int, int]] = []
        self._secret_content = False
        self._governed: tuple[list[tuple[_Cell, str]], list[_Cell]] | None = None

    @classmethod
    def run(cls, source: str) -> _FixtureLocator:
        locator = cls(source)
        locator.feed(source)
        locator.close()
        return locator

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _boundary(self) -> None:
        self._piece_positions.append(self._offset())
        self._pieces.append(" ")

    def text_between(self, start: int, end: int) -> str:
        """The normalised text of ``[start, end)``: every tag boundary a space, as the parser reads a
        cell. A cell with more text than the parser's text bound is not read (``FixtureBoundsError``)."""
        index = bisect_left(self._piece_positions, start)
        parts: list[str] = []
        size = 0
        while index < len(self._pieces) and self._piece_positions[index] < end:
            if len(parts) >= MAX_TEXT_PARTS or size > MAX_TEXT_CHARS:
                raise FixtureBoundsError("a table cell holds more text than the parser reads; not sanitized")
            part = self._pieces[index]
            parts.append(part)
            size += len(part)
            index += 1
        if size > MAX_TEXT_CHARS:
            raise FixtureBoundsError("a table cell holds more text than the parser reads; not sanitized")
        return normalize_whitespace("".join(parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._elements += 1
        if self._elements > MAX_ELEMENTS:
            raise FixtureBoundsError("the page has more elements than the parser reads; not sanitized")
        self._boundary()
        self.expect_raw_content(tag)
        values = {key: value or "" for key, value in attrs}
        self._table_start(tag, values)
        if tag in ("input", "textarea", "select", "button"):
            self._control_start(tag, values)
        elif tag in ("form", "a"):
            self._target_start()

    def _target_start(self) -> None:
        """A form or link whose target holds a secret query value is rewritten in its start tag."""
        start = self._offset()
        text = self.get_starttag_text() or ""

        def redact(match: re.Match[str]) -> str:
            raw = match.group(2)
            quote = raw[0] if raw[:1] in ("'", '"') else ""
            inner = raw[1:-1] if quote else raw
            redacted = redact_href(inner)
            if redacted == inner:
                return match.group(0)
            self.secret_values.extend(href_secret_values(inner))
            return f"{match.group(1)}{quote}{redacted}{quote}"

        rewritten = _TARGET_ATTR.sub(redact, text)
        if rewritten != text:
            self.tag_edits.append((start, start + len(text), rewritten))

    def _control_start(self, tag: str, values: dict[str, str]) -> None:
        start = self._offset()
        end = start + len(self.get_starttag_text() or "")
        name = values.get("name") or values.get("id") or ""
        control_type = (values.get("type") or "text") if tag == "input" else tag
        if tag == "button" or (tag == "input" and control_type.lower() in _BUTTON_INPUT_TYPES):
            self.buttons.append(_Control(tag, start, end))
        # A password-type input is secret even without a name or id (the parser skips it, so the
        # include_secrets residue parse cannot see it either).
        password_input = tag == "input" and control_type.lower() == "password"
        if password_input and values.get("value", "").strip() not in ("", _REDACTED):
            self.password_values = True
        if tag != "button" and control_type.lower() not in _NOT_DATA_INPUT_TYPES:
            control = _Control(tag, start, end)
            self.controls.append(control)
            if tag != "input":
                self._open_control[tag] = control
        if tag == "button" or not (
            (name and is_sensitive_fixture_control(name, control_type)) or password_input
        ):
            return
        self._secret_content = password_input or is_sensitive_name(name)
        if tag == "input":
            self.tag_spans.append((start, end))
            if self._secret_content:
                self.secret_values.append(values.get("value", ""))
        elif tag == "textarea":
            self._textarea_content_start = end
        else:
            self._select_content_start = end

    def _table_start(self, tag: str, values: dict[str, str]) -> None:
        offset = self._offset()
        if tag == "table":
            if len(self._stack) >= MAX_NESTING:
                raise FixtureBoundsError("tables are nested deeper than the parser reads; not sanitized")
            for enclosing in self._stack:
                if enclosing.cell is not None:
                    enclosing.cell.has_table = True
            self._stack.append(_Table())
            return
        if not self._stack:
            return
        table = self._stack[-1]
        if tag == "tr":
            table.close_cell(offset)
            table.rows.append([])
            table.row_open = True
        elif tag in ("td", "th"):
            table.close_cell(offset)
            if table.row_open:
                spans = {name: raw_span_value(values.get(name)) for name in ("colspan", "rowspan")}
                table.cell = _Cell(
                    start=offset + len(self.get_starttag_text() or ""),
                    spans=spans,
                    header=tag == "th",
                    owner=self,
                )
                table.rows[-1].append(table.cell)
        elif tag in CONTROL_TAGS:
            for enclosing in self._stack:
                if enclosing.cell is not None:
                    enclosing.cell.has_control = True

    def handle_endtag(self, tag: str) -> None:
        self._boundary()
        offset = self._offset()
        if tag in self._open_control:
            self._open_control.pop(tag).content_end = offset
        if tag == "textarea" and self._textarea_content_start is not None:
            self.text_spans.append((self._textarea_content_start, offset))
            if self._secret_content:
                self.secret_spans.append((self._textarea_content_start, offset))
            self._textarea_content_start = None
        if tag == "select" and self._select_content_start is not None:
            self.select_spans.append((self._select_content_start, offset))
            if self._secret_content:
                self.secret_spans.append((self._select_content_start, offset))
            self._select_content_start = None
        if not self._stack:
            return
        table = self._stack[-1]
        if tag in ("td", "th"):
            table.close_cell(offset)
        elif tag == "tr":
            table.close_cell(offset)
            table.row_open = False
        elif tag == "table":
            table.close_cell(offset)
            self.tables.append(self._stack.pop())

    def handle_data(self, data: str) -> None:
        self._piece_positions.append(self._offset())
        self._pieces.append(data)

    def close(self) -> None:
        super().close()
        while self._stack:
            table = self._stack.pop()
            table.close_cell(self._source_length)
            self.tables.append(table)
        for control in self._open_control.values():
            control.content_end = self._source_length

    def governed(self) -> tuple[list[tuple[_Cell, str]], list[_Cell]]:
        """``(cells the parser redacts as (cell, replacement), label cells that name a secret)`` of every
        table (``parser.govern_table`` on the same laid-out grid), computed once."""
        if self._governed is None:
            hits: list[tuple[_Cell, str]] = []
            labels: list[_Cell] = []
            for table in self.tables:
                grid, cut = layout_table_bounded(table.rows, lambda cell, name: cell.spans[name], MAX_GRID_CELLS)
                if cut or any(row.clamped for row in grid):
                    raise FixtureBoundsError("a table is larger than the parser lays out; not sanitized")
                cells, label_cells = govern_table(
                    grid, lambda cell: cell.text, header_row_is_data=True, is_header_cell=lambda cell: cell.header
                )
                hits += cells
                labels += label_cells
            self._governed = (hits, labels)
        return self._governed

    @property
    def table_residue(self) -> bool:
        """True when a cell the parser would redact still holds its text (a value the sanitizer
        missed, whatever the cell's position on the table grid)."""
        return any(
            cell.text not in ("", _REDACTED, replacement) for cell, replacement in _govern_cells(self)
        )


def _govern_cells(locator: _FixtureLocator) -> list[tuple[_Cell, str]]:
    """Every table cell the parser redacts as ``(cell, replacement)`` whose content is replaced as a
    whole. Cells holding a form control are handled by their controls and text runs (see
    ``_labelled_control_edits`` and ``_governed_text_edits``), and a label cell that holds a nested
    table is left to the cells of that table."""
    return [
        (cell, replacement)
        for cell, replacement in locator.governed()[0]
        if not (cell.has_control or (replacement != _REDACTED and cell.has_table))
    ]


def _labelled_control_cells(locator: _FixtureLocator) -> list[_Cell]:
    """Control-bearing value cells of a secret-labelled row and label cells that name a secret, by position."""
    hits, labels = locator.governed()
    cells = [cell for cell, replacement in hits if replacement == _REDACTED] + labels
    return sorted((cell for cell in cells if cell.has_control), key=lambda cell: cell.start)


def _labelled_controls(locator: _FixtureLocator) -> list[_Control]:
    """The controls and buttons inside a value cell of a secret-labelled row, or inside a label cell that
    names a secret. Cells are looked up by position (a prefix maximum of their ends), not scanned."""
    cells = _labelled_control_cells(locator)
    starts = [cell.start for cell in cells]
    reach = list(accumulate((cell.end for cell in cells), max))

    def inside(position: int) -> bool:
        index = bisect_right(starts, position) - 1
        return index >= 0 and reach[index] > position

    return [control for control in (*locator.controls, *locator.buttons) if inside(control.start)]


def _labelled_control_edits(source: str, locator: _FixtureLocator) -> list[Edit]:
    """Edits redacting every control (and button value) inside a value cell of a secret-labelled row, or
    inside a label cell that names a secret, as the parser treats those controls as secret whatever
    they are named."""
    edits: list[Edit] = []
    for control in _labelled_controls(locator):
        if control.tag in ("input", "button"):
            tag = source[control.start : control.end]
            edits.append((control.start, control.end, _VALUE_ATTR.sub(lambda m: f'{m.group(1)}"{_REDACTED}"', tag)))
        elif control.tag == "textarea":
            if source[control.end : control.content_end].strip():
                edits.append((control.end, control.content_end, _REDACTED))
        else:
            content = source[control.end : control.content_end]
            edits.append((control.end, control.content_end, _OPTION_PART.sub(_redact_option_part, content)))
    return edits


def _text_runs(source: str, start: int, end: int) -> list[Edit]:
    """The text runs between the tags of ``source[start:end]`` that hold more than whitespace, each as
    an edit replacing it with the placeholder."""
    return [
        (match.start(), match.end(), _REDACTED)
        for match in _MARKUP_TOKEN.finditer(source, start, end)
        if match.group("text") and match.group("text").strip()
    ]


def _outermost(cells: list[_Cell]) -> list[_Cell]:
    """The cells that are not inside another of ``cells``."""
    outer: list[_Cell] = []
    reach = -1
    for cell in sorted(cells, key=lambda cell: (cell.start, -cell.end)):
        if cell.start < reach:
            continue
        reach = cell.end
        outer.append(cell)
    return outer


def _governed_text_edits(source: str, locator: _FixtureLocator) -> list[Edit]:
    """Edits redacting the text (hint words, a copy of the secret, a button caption) of a value cell of
    a secret-labelled row that also holds a control: the control's value is redacted by
    ``_labelled_control_edits``, and what the cell says around it is as secret as the cell."""
    cells = [cell for cell, replacement in locator.governed()[0] if replacement == _REDACTED and cell.has_control]
    return [edit for cell in _outermost(cells) for edit in _text_runs(source, cell.start, cell.end)]


def _sensitive_cell_spans(value: str, locator: _FixtureLocator) -> list[Edit]:
    """Cell contents to replace, as ``(start, end, replacement)``: the cells ``sensitive_table_cells``
    names, with nested spans folded into their outermost cell."""
    spans = {(cell.start, cell.end, replacement) for cell, replacement in _govern_cells(locator)}
    outermost: list[Edit] = []
    for start, end, replacement in sorted(span for span in spans if value[span[0] : span[1]].strip()):
        if outermost and start < outermost[-1][1]:
            continue
        outermost.append((start, end, replacement))
    return outermost


def _all_edits(value: str, locator: _FixtureLocator) -> list[Edit]:
    """Every edit the sanitizer makes to the markup, outermost first and without overlaps: where two
    edits overlap the one that starts first (and, from the same start, the longer) is kept."""
    edits: set[Edit] = set(_sensitive_cell_spans(value, locator))
    for start, end in locator.tag_spans:
        tag = value[start:end]
        edits.add((start, end, _VALUE_ATTR.sub(lambda m: f'{m.group(1)}"{_REDACTED}"', tag)))
    for start, end in locator.text_spans:
        if value[start:end].strip():
            edits.add((start, end, _REDACTED))
    for start, end in locator.select_spans:
        edits.add((start, end, _OPTION_PART.sub(_redact_option_part, value[start:end])))
    edits.update(_labelled_control_edits(value, locator))
    edits.update(_governed_text_edits(value, locator))
    edits.update(locator.tag_edits)
    kept: list[Edit] = []
    reach = 0
    for edit in sorted(edits, key=lambda edit: (edit[0], -edit[1], edit[2])):
        if edit[0] < reach:
            continue
        kept.append(edit)
        reach = edit[1]
    return kept


def _redact_sensitive_controls(value: str) -> str:
    locator = _FixtureLocator.run(value)
    pieces: list[str] = []
    position = 0
    for start, end, replacement in _all_edits(value, locator):
        pieces += [value[position:start], replacement]
        position = end
    pieces.append(value[position:])
    return "".join(pieces)


# Inside a sensitive select: a tag (option tags get their value attribute redacted) or the text
# between tags (an option label, which is also its value when the tag has no value attribute).
_OPTION_PART = re.compile(r"(<[^>]*>)|([^<]+)")


def _redact_option_part(match: re.Match[str]) -> str:
    tag, text = match.group(1), match.group(2)
    if tag is not None:
        if re.match(r"<option\b", tag, re.IGNORECASE):
            return _VALUE_ATTR.sub(lambda m: f'{m.group(1)}"{_REDACTED}"', tag)
        return tag
    return _REDACTED if text.strip() else text


# Record keys that name a row or its grouping rather than hold one of its values.
_ROW_LABEL_KEYS = frozenset({"Metric", "Section", "Line"})


def sensitive_control_residue(parsed: ParsedPage) -> list[str]:
    """Names of sensitive controls, and labels of secret table values, that still carry a value in
    an include_secrets parse; also buttons the parser flags sensitive, and links and form actions
    that carry a secret query value."""
    controls = [(f.name, f.type, f.value, f.sensitive) for f in parsed.fields]
    controls += [(t.name, "textarea", t.value, t.sensitive) for t in parsed.textareas]
    for select in parsed.selects:
        options = select.option_details or []
        controls.append((select.name, "select", select.value, select.sensitive))
        controls += [(select.name, "select", text, select.sensitive) for o in options for text in (o.value, o.label)]
    # The parser's own flag covers a control it governs by its row label rather than its name.
    residue = [
        name
        for name, control_type, value, flagged in controls
        if (is_sensitive_fixture_control(name, control_type) or (flagged and control_type not in _NOT_DATA_INPUT_TYPES))
        and value.strip() not in ("", _REDACTED)
    ]
    residue += [
        button.name or "button"
        for button in parsed.buttons
        if button.sensitive and any(text.strip() not in ("", _REDACTED) for text in (button.value, button.label))
    ]
    residue += ["link target" for link in parsed.links or [] if redact_href(link.href) != link.href]
    residue += ["form action" for form in parsed.forms if redact_href(form.action) != form.action]
    # Labelled table values (value entries and wide-table records) the parser redacts by label,
    # the default a secret label carries, and every value of a record whose row label (its Metric,
    # else its first cell) names a secret, such as the second line's Phone Number.
    labelled = [(entry.label, entry.value) for entry in parsed.value_entries or []]
    labelled += [(entry.label, entry.default_value or "") for entry in parsed.value_entries or []]
    labelled += [(key, value) for record in parsed.tables for key, value in record.items()]
    for record in parsed.tables:
        label_key = "Metric" if "Metric" in record else next(iter(record), "")
        row_label = record.get(label_key, "")
        labelled += [
            (row_label, value) for key, value in record.items() if key != label_key and key not in _ROW_LABEL_KEYS
        ]
    residue += [label for label, value in labelled if is_sensitive_name(label) and value.strip() not in ("", _REDACTED)]
    return list(dict.fromkeys(residue))


def sanitize_router_fixture(value: str) -> str:
    value = _redact_sensitive_controls(value)
    value = _MAC.sub("[redacted-mac]", value)
    value = _IPV4.sub("[redacted-ip]", value)
    value = _IPV6_FULL.sub("[redacted-ipv6]", value)
    value = _IPV6_COMPRESSED.sub("[redacted-ipv6]", value)
    value = _NAME_THEN_VALUE.sub(r"\1[redacted]\2", value)
    value = _VALUE_THEN_NAME.sub(r"\1[redacted]\2", value)
    value = _LABEL_DEFAULT.sub(r"\1[redacted]", value)
    value = _IDENTITY_CELL.sub(r"\1[redacted]\2", value)
    value = _IP_SLASH_NAME.sub(r"\1[redacted-name]", value)
    return _redact_names_before_macs(value)


def _redact_names_before_macs(value: str) -> str:
    """``name/[redacted-mac]`` -> ``[redacted-name]/[redacted-mac]``: the name is the run of text before
    the slash back to the previous tag, line break, slash or earlier marker. Scanned backwards from each
    marker and never past the end of the previous one (each marker is consumed once), so a long run
    costs one pass, not one per start position."""
    pieces: list[str] = []
    position = 0
    floor = 0
    index = value.find(_SLASH_MAC)
    while index != -1:
        start = index
        while start > floor and value[start - 1] not in _NAME_BREAKS:
            start -= 1
        if start < index:
            pieces += [value[position:start], "[redacted-name]"]
            position = index
            floor = index + len(_SLASH_MAC)  # a marker that took a name is consumed; the next name starts after it
        index = value.find(_SLASH_MAC, index + len(_SLASH_MAC))
    return "".join(pieces) + value[position:]


def _markup_residue(value: str) -> bool:
    """A password-type input (named or not) whose value attribute is not redacted, a table cell under
    a secret label or header that still holds its text, a control or the words around it in such a
    cell, or a form or link target that still carries a secret query value."""
    locator = _FixtureLocator.run(value)
    pending = [*_labelled_control_edits(value, locator), *_governed_text_edits(value, locator), *locator.tag_edits]
    changed = any(value[start:end] != text for start, end, text in pending)
    return locator.password_values or locator.table_residue or changed


def contains_sensitive_fixture_value(value: str, *, html: bool = True) -> bool:
    """Fail-closed residue check on serialized text. ``html=False`` (JSON and other non-markup
    text) skips the markup parse that looks for unredacted password-type inputs and secret cells."""
    return bool(
        _RESIDUE_SENSITIVE_JSON.search(value)
        or _RESIDUE_LABEL_DEFAULT.search(value)
        or _RESIDUE_HASH.search(value)
        or _MAC.search(value)
        or (html and _markup_residue(value))
    )


def _decoded_pieces(content: str) -> list[str]:
    """The text and the ``value`` attributes inside ``content`` (a textarea's text, a select's options), decoded."""
    pieces = [text for text in re.split(r"<[^>]*>", content)]
    for match in _VALUE_ATTR_TEXT.finditer(content):
        pieces.append(next(group for group in match.groups() if group is not None))
    return [html_module.unescape(piece).strip() for piece in pieces if piece.strip()]


def fixture_secret_values(source: str) -> list[str]:
    """The secrets found in ``source``, as the literal strings (and their HTML-escaped spellings) the
    sanitized page must not contain: the values of password-class controls, the controls and the words of
    a secret-labelled cell, and secret query values in form and link targets. Values shorter than a few
    characters are left out (they would match ordinary text). Raises ``FixtureBoundsError`` past a bound."""
    locator = _FixtureLocator.run(source)
    found: list[str] = list(locator.secret_values)
    for start, end in locator.secret_spans:
        found += _decoded_pieces(source[start:end])
    # A cell redacted as a whole is the secret itself; the words around a control in a governed cell
    # are hints as often as copies of the secret, and the sanitizer redacts them without a literal check.
    for cell, replacement in _govern_cells(locator):
        if replacement == _REDACTED:
            found += [html_module.unescape(source[s:e]).strip() for s, e, _ in _text_runs(source, cell.start, cell.end)]
    for control in _labelled_controls(locator):
        if control.tag in ("input", "button"):
            found += [
                html_module.unescape(next(group for group in match.groups() if group is not None))
                for match in _VALUE_ATTR_TEXT.finditer(source[control.start : control.end])
            ]
        else:
            found += _decoded_pieces(source[control.end : control.content_end])
    variants: dict[str, None] = {}
    for secret in found:
        if len(secret) >= _MIN_LITERAL_SECRET and secret != _REDACTED:
            variants[secret] = None
            variants[html_module.escape(secret, quote=False)] = None
            variants[html_module.escape(secret)] = None
    return list(variants)
