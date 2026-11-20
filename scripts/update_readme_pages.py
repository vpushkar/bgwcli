"""Rewrite the generated blocks in README.md from bgwcli.helpdoc (pages, include pages, actions).

Run from the repository root: .venv/bin/python -I scripts/update_readme_pages.py
Exits 1 without writing anything when any marker pair is missing, duplicated, misordered or overlapping.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bgwcli import helpdoc  # noqa: E402


def _validate(text: str, names: list[str]) -> tuple[list[str], list[tuple[int, int, str]]]:
    """Return (problems, spans) where spans are (start_index, end_index_after_end_marker, name)."""
    problems: list[str] = []
    spans: list[tuple[int, int, str]] = []
    for name in names:
        start, end = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
        n_start, n_end = text.count(start), text.count(end)
        if n_start != 1:
            problems.append(f"block '{name}': expected exactly one start marker, found {n_start}")
        if n_end != 1:
            problems.append(f"block '{name}': expected exactly one end marker, found {n_end}")
        if n_start == 1 and n_end == 1:
            s_idx, e_idx = text.index(start), text.index(end)
            if e_idx < s_idx + len(start):
                problems.append(f"block '{name}': end marker comes before start marker")
            else:
                spans.append((s_idx, e_idx + len(end), name))
    spans.sort()
    for (_, prev_end, prev_name), (nxt_start, _, nxt_name) in zip(spans, spans[1:], strict=False):
        if nxt_start < prev_end:
            problems.append(f"blocks '{prev_name}' and '{nxt_name}' overlap or are nested")
    return problems, spans


def rewrite(text: str, blocks: dict[str, str]) -> str:
    """Replace each block's content; raise ValueError (one problem per line) on any marker violation."""
    problems, _ = _validate(text, list(blocks))
    if problems:
        raise ValueError("\n".join(problems))
    for name, content in blocks.items():
        start, end = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        text = head + start + content + end + tail
    return text


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
