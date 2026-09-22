"""Span-aware HTML table -> rectangular grid.

SEBI's PMR tables use nested ``colspan``/``rowspan`` headers (up to three header
rows deep).  Reading them positionally is brittle, so every table is first
expanded into a dense 2-D grid where a spanned cell is repeated across every
position it physically occupies.  Column names are then derived by joining the
header rows, which keeps the parser working when SEBI reorders or inserts a
column.
"""
from __future__ import annotations

import re
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CELL = re.compile(r"<(t[dh])\b([^>]*)>(.*?)</\1\s*>", re.I | re.S)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.I | re.S)
_TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
_SPAN = re.compile(r'(colspan|rowspan)\s*=\s*"?\'?(\d+)', re.I)

_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'", "&rsquo;": "'",
}


def clean(html: str) -> str:
    """Strip tags/entities from a cell and normalise whitespace."""
    txt = _TAG.sub(" ", html)
    for ent, rep in _ENTITIES.items():
        txt = txt.replace(ent, rep)
    txt = txt.replace("\xa0", " ")
    return _WS.sub(" ", txt).strip()


def _spans(attrs: str) -> tuple[int, int]:
    col = row = 1
    for name, val in _SPAN.findall(attrs):
        n = max(1, min(int(val), 60))  # clamp: SEBI occasionally emits colspan="100"
        if name.lower() == "colspan":
            col = n
        else:
            row = n
    return col, row


def expand(table_html: str) -> list[list[str]]:
    """Expand one ``<table>`` into a dense grid of cell texts."""
    grid: list[list[str | None]] = []
    for r_idx, row_html in enumerate(_ROW.findall(table_html)):
        while len(grid) <= r_idx:
            grid.append([])
        row = grid[r_idx]
        col = 0
        for tag, attrs, body in _CELL.findall(row_html):
            while col < len(row) and row[col] is not None:
                col += 1  # skip positions already claimed by a rowspan above
            cspan, rspan = _spans(attrs)
            text = clean(body)
            for dr in range(rspan):
                while len(grid) <= r_idx + dr:
                    grid.append([])
                target = grid[r_idx + dr]
                for dc in range(cspan):
                    pos = col + dc
                    while len(target) <= pos:
                        target.append(None)
                    if target[pos] is None:
                        target[pos] = text
            col += cspan
    width = max((len(r) for r in grid), default=0)
    return [[(c if c is not None else "") for c in r] + [""] * (width - len(r)) for r in grid]


def expand_with_tags(table_html: str) -> tuple[list[list[str]], list[list[str]]]:
    """Like :func:`expand` but also returns the originating tag (``td``/``th``)."""
    grid: list[list[str | None]] = []
    tags: list[list[str | None]] = []
    for r_idx, row_html in enumerate(_ROW.findall(table_html)):
        while len(grid) <= r_idx:
            grid.append([])
            tags.append([])
        row = grid[r_idx]
        col = 0
        for tag, attrs, body in _CELL.findall(row_html):
            while col < len(row) and row[col] is not None:
                col += 1
            cspan, rspan = _spans(attrs)
            text = clean(body)
            for dr in range(rspan):
                while len(grid) <= r_idx + dr:
                    grid.append([])
                    tags.append([])
                trow, grow = tags[r_idx + dr], grid[r_idx + dr]
                for dc in range(cspan):
                    pos = col + dc
                    while len(grow) <= pos:
                        grow.append(None)
                        trow.append(None)
                    if grow[pos] is None:
                        grow[pos] = text
                        trow[pos] = tag.lower()
            col += cspan
    width = max((len(r) for r in grid), default=0)
    g = [[(c if c is not None else "") for c in r] + [""] * (width - len(r)) for r in grid]
    t = [[(c if c is not None else "td") for c in r] + ["td"] * (width - len(r)) for r in tags]
    return g, t


_NUM = re.compile(r"^[\s(]*[-+]?[\d,]*\.?\d+\s*\)?%?$")


def is_number(text: str) -> bool:
    """True when the whole cell is a bare number (SEBI never units its figures)."""
    t = text.strip()
    return bool(t) and bool(_NUM.match(t)) and any(ch.isdigit() for ch in t)


def to_number(text: str):
    """Parse a SEBI figure to float; ``None`` when blank/NA. '(12)' means -12."""
    t = (text or "").strip().replace(",", "").replace("%", "")
    if not t or t.upper() in {"NA", "N.A.", "N/A", "-", "--", "NIL"}:
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        val = float(t)
    except ValueError:
        return None
    return -val if neg else val


def header_depth(grid: list[list[str]], tags: list[list[str]] | None = None, cap: int = 5) -> int:
    """Leading rows with no bare-numeric cell are the header.

    SEBI marks many data cells as ``<th>``, so the tag alone cannot be trusted;
    the presence of a bare number is what actually separates body from header.
    """
    for i, row in enumerate(grid):
        if i >= cap:
            return cap
        if any(is_number(c) for c in row):
            return i
    return len(grid)
