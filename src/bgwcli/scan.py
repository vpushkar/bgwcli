"""Compatibility view over the sweep engine: compact metadata with fallbacks enabled."""

from __future__ import annotations

from typing import Any

from .sweep import SweepOptions, SweepPage, sweep_router

PageScan = SweepPage


def scan_router(
    client: Any,
    *,
    delay_ms: int,
    include_parsed: bool = False,
    include_forms: bool = False,
    pages: list[str] | None = None,
) -> list[PageScan]:
    return sweep_router(
        client,
        SweepOptions(
            delay_ms=delay_ms,
            pages=pages,
            include_parsed=include_parsed,
            include_forms=include_forms,
            use_fallbacks=True,
        ),
    )
