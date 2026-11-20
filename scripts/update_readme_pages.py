"""Rewrite the generated blocks in README.md from bgwcli.helpdoc (pages, include pages, actions).

Run from the repository root: .venv/bin/python -I scripts/update_readme_pages.py
Exits 1 without writing anything when any marker pair is missing, duplicated, misordered or overlapping.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bgwcli import helpdoc  # noqa: E402


def _validate(text: str, names: list[str]) -> tuple[list[str], list[tuple[int, int, str]]]:
    """Return (problems, spans) where spans are (start_index, end_index_after_end_marker, name).

    Marker tokens are found whitespace-insensitively; any token that is not the exact canonical form
    (start: at line start, followed by a newline; end: at line start) is reported, never ignored.
    """
    problems: list[str] = []
    spans: list[tuple[int, int, str]] = []
    for name in names:
        start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
        starts: list[int] = []
        ends: list[int] = []
        malformed = 0
        for m in re.finditer(rf"<!--\s*{re.escape(name)}:(start|end)\s*-->", text):
            is_start = m.group(1) == "start"
            at_line_start = m.start() == 0 or text[m.start() - 1] == "\n"
            canonical = m.group(0) == (start if is_start else end) and at_line_start
            if is_start:
                canonical = canonical and text.startswith("\n", m.end())
            if canonical:
                (starts if is_start else ends).append(m.start())
            else:
                malformed += 1
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"block '{name}': ambiguous/malformed {m.group(1)} marker at line {line}")
        if malformed:
            continue
        if len(starts) != 1:
            problems.append(f"block '{name}': expected exactly one start marker, found {len(starts)}")
        if len(ends) != 1:
            problems.append(f"block '{name}': expected exactly one end marker, found {len(ends)}")
        if len(starts) == 1 and len(ends) == 1:
            if ends[0] < starts[0] + len(start) + 1:
                problems.append(f"block '{name}': end marker comes before start marker")
            else:
                spans.append((starts[0], ends[0] + len(end), name))
    spans.sort()
    for (_, prev_end, prev_name), (nxt_start, _, nxt_name) in zip(spans, spans[1:], strict=False):
        if nxt_start < prev_end:
            problems.append(f"blocks '{prev_name}' and '{nxt_name}' overlap or are nested")
    return problems, spans


def rewrite(text: str, blocks: dict[str, str]) -> str:
    """Replace each block's content; raise ValueError (one problem per line) on any marker violation."""
    problems, spans = _validate(text, list(blocks))
    if problems:
        raise ValueError("\n".join(problems))
    out: list[str] = []
    pos = 0
    for s_idx, e_end, name in spans:
        start, end = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
        out.append(text[pos : s_idx + len(start)])
        out.append(blocks[name])
        out.append(end)
        pos = e_end
    out.append(text[pos:])
    return "".join(out)


def main(readme_path: Path | None = None) -> int:
    readme = readme_path if readme_path is not None else ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    blocks = {name: getattr(helpdoc, renderer)() for name, renderer in helpdoc.README_BLOCKS.items()}
    try:
        text = rewrite(text, blocks)
    except ValueError as exc:
        for line in str(exc).splitlines():
            sys.stderr.write(f"{readme.name}: {line}\n")
        return 1
    readme.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
