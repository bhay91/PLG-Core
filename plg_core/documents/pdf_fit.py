from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
from typing import Callable, Iterator


_COMPACT_LEVEL: ContextVar[int] = ContextVar("pps_pdf_compact_level", default=0)


def compact_level() -> int:
    return _COMPACT_LEVEL.get()


def fit_value(normal: float, compact: float, tight: float, minimum: float) -> float:
    return (normal, compact, tight, minimum)[min(compact_level(), 3)]


def font_scale() -> float:
    # Spacing and padding are exhausted before readable type is reduced.
    return (1.0, 1.0, 1.0, 0.93)[min(compact_level(), 3)]


def page_count(path: Path) -> int:
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page(?!s)\b", data))


@contextmanager
def using_compact_level(level: int) -> Iterator[None]:
    token = _COMPACT_LEVEL.set(level)
    try:
        yield
    finally:
        _COMPACT_LEVEL.reset(token)


def build_with_one_page_preference(
    path: Path,
    builder: Callable[[], None],
    *,
    maximum_level: int = 3,
) -> tuple[int, int]:
    """Build at master geometry, tightening only when the PDF needs it."""
    final_level = 0
    final_pages = 0
    for level in range(maximum_level + 1):
        with using_compact_level(level):
            builder()
        final_level = level
        final_pages = page_count(path)
        if final_pages <= 1:
            break
    return final_level, final_pages
