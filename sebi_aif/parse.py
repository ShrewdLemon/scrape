"""Extract the Category III rows from SEBI's AIF statistics page.

Source: https://www.sebi.gov.in/statistics/1392982252002.html

The page is one static document holding every quarter since September 2012,
newest first.  Each quarter's section opens with "Cumulative net figures as at
the end of ..." and two of its tables are extracted here:

* that headline table - Commitments Raised, Funds Raised, Investments Made -
  published for every quarter;
* "Cumulative net investment made in equity and debt securities", published
  only from the quarter ending March 31, 2024.

Three quirks of the page shape the parser:

1. Retired content is left in HTML comments (including a whole table), so
   comments are stripped before anything is located.
2. The September 2020, 2021, 2022 and 2023 sections are headed "December 31"
   of the same year - a copy of the heading above them.  Sections run strictly
   newest first, one quarter apart, so a repeated date is re-dated to the
   quarter before it and flagged; any other break in the sequence fails loudly
   rather than being guessed at.
3. Formatting drifts over the years ("March 31 st , 2024", "30th June 2018",
   "1,28,058.26", "46824.91"), so dates are parsed leniently and columns are
   matched on their header text, never by position alone.
"""
from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from sebi_pmr.tables import clean, expand, to_number

log = logging.getLogger(__name__)

URL = "https://www.sebi.gov.in/statistics/1392982252002.html"

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TABLE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
_SECTION = re.compile(r"Cumulative\s+net\s+figures\s+as\s+at\s+the\s+end\s+of", re.I)
_EQUITY_DEBT = re.compile(
    r"Cumulative\s+net\s+investment\s+made\s+in\s+equity\s+and\s+debt\s+securities", re.I)
_NEXT_HEADING = re.compile(r"Cumulative\s+net\s+investment", re.I)
_HEADER_ROW = re.compile(r"^Category(\s+of\s+AIFs?)?$", re.I)
_BODY_ROW = re.compile(r"^Category\s+I", re.I)
_CATEGORY_III = re.compile(r"^Category\s+III\b", re.I)
_MARKER = re.compile(r"\s*\(\s*[*#]+\s*\)\s*$")          # the (*) / (#) footnote markers
_NIL = {"", "-", "--", "–", "—", "nil", "na", "n.a.", "n/a"}
_BAD_GROUPING = re.compile(r",\d{1,2}(?:\.\d*)?$")       # "5,63": a digit lost after the comma

_MONTHS = {}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i
_MONTHS["sept"] = 9

_ORD = r"(?:\s*(?:st|nd|rd|th)\b)?"
_DATE = re.compile(
    rf"\b(?P<m1>[A-Za-z]{{3,9}})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s*,?\s*(?P<y1>\d{{4}})"
    rf"|\b(?P<d2>\d{{1,2}}){_ORD}\s+(?P<m2>[A-Za-z]{{3,9}})\s*,?\s*(?P<y2>\d{{4}})")

#: Output column -> header pattern, for the headline "Cumulative net figures" table.
NET_FIGURES = {
    "Commitments Raised": re.compile(r"commitment", re.I),
    "Funds Raised": re.compile(r"funds?\s+raised", re.I),
    "Investments Made": re.compile(r"investments?\s+made", re.I),
}

#: Output column -> header pattern, for "Cumulative net investment made in
#: equity and debt securities".  Names follow SEBI's own wording.
EQUITY_DEBT = {
    "Investments made in equity/equity linked securities": re.compile(r"equity", re.I),
    "Investment made in debt securities": re.compile(r"debt", re.I),
    "Investment in units of AIFs/REITs/InVITs": re.compile(r"units\s+of|REIT|InvIT", re.I),
    "Security Receipts": re.compile(r"security\s+receipt", re.I),
}


class FormatError(ValueError):
    """The page no longer looks the way this parser expects."""


@dataclass
class Quarter:
    """The Category III rows of one quarter's section."""

    heading: str                        # SEBI's section heading, as printed
    stated_date: date | None            # the date that heading states
    net_label: str                      # "Category III AIF" / "Category III"
    net: dict[str, float | None]
    equity_debt_label: str | None = None
    equity_debt: dict[str, float | None] | None = None
    footnote: str | None = None         # SEBI's (*)/(#) definitions under the equity/debt table
    quarter_end: date | None = None     # the quarter this section actually covers
    notes: list[str] = field(default_factory=list)


def heading_date(text: str) -> tuple[date | None, int]:
    """The first real date in ``text`` and where it ends (``None, -1`` if none)."""
    for m in _DATE.finditer(text):
        month = _MONTHS.get((m["m1"] or m["m2"]).lower())
        if not month:
            continue
        try:
            return date(int(m["y1"] or m["y2"]), month, int(m["d1"] or m["d2"])), m.end()
        except ValueError:
            continue
    return None, -1


def previous_quarter(d: date) -> date:
    y, m = (d.year, d.month - 3) if d.month > 3 else (d.year - 1, d.month + 9)
    return date(y, m, calendar.monthrange(y, m)[1])


def is_quarter_end(d: date) -> bool:
    return d.month % 3 == 0 and d.day == calendar.monthrange(d.year, d.month)[1]


def _value(raw: str, notes: list[str], where: str) -> float | None:
    """Parse one figure; '-' means nil.  Unreadable text fails rather than blanking."""
    if raw.strip().lower() in _NIL:
        return None
    val = to_number(raw)
    if val is None:
        raise FormatError(f"{where}: unreadable figure {raw!r}")
    if _BAD_GROUPING.search(raw.strip()):
        notes.append(f"{where} is printed as {raw.strip()!r} (irregular digit grouping); "
                     f"read as {val:g}")
    return val


def _map_columns(names: list[str], spec: dict[str, re.Pattern]) -> list[str]:
    """Output name per column; a header nothing in ``spec`` claims is kept as printed."""
    out, used = [], set()
    for name in names:
        label = _MARKER.sub("", name)
        hit = next((k for k, pat in spec.items() if k not in used and pat.search(label)), None)
        if hit:
            used.add(hit)
        out.append(hit or label)
    missing = [k for k in spec if k not in used]
    if missing:
        raise FormatError(f"columns {missing} not found among headers {names}")
    return out


def category_iii(table_html: str, spec: dict[str, re.Pattern],
                 notes: list[str], where: str) -> tuple[str, dict[str, float | None]]:
    """The one Category III row of a table, keyed by output column name."""
    grid = expand(table_html)
    body = next((i for i, r in enumerate(grid) if _BODY_ROW.match(r[0])), None)
    heads = [r for r in grid[:body] if _HEADER_ROW.match(r[0])] if body is not None else []
    if not heads:
        raise FormatError(f"{where}: no 'Category of AIF' header row")
    cols, names = [], []
    for c in range(1, len(grid[0])):
        parts = []
        for r in heads:
            if r[c] and r[c] not in parts:
                parts.append(r[c])
        if parts:                                 # a column with no header is layout, not data
            cols.append(c)
            names.append(" ".join(parts))
    out_names = _map_columns(names, spec)
    rows = [r for r in grid[body:] if _CATEGORY_III.match(r[0])]
    if len(rows) != 1:
        raise FormatError(f"{where}: expected one Category III row, found {len(rows)}")
    row = rows[0]
    return row[0], {name: _value(row[c], notes, f"{where} / {name}")
                    for c, name in zip(cols, out_names)}


def _assign_dates(sections: list[Quarter]) -> None:
    """Set ``quarter_end`` for sections given newest first (see quirk 2 above)."""
    claimed = {q.stated_date for q in sections if q.stated_date}
    prev = None
    for q in sections:
        d = q.stated_date
        if d is None:
            raise FormatError(f"no date in heading {q.heading!r}")
        want = previous_quarter(prev) if prev else None
        if want is None:
            if not is_quarter_end(d):
                raise FormatError(f"newest heading {q.heading!r} is not a quarter end")
            q.quarter_end = d
        elif d == want:
            q.quarter_end = d
        elif d == prev and want not in claimed:
            q.quarter_end = want
            q.notes.append(f"SEBI's heading repeats the quarter above it ({d:%B %d, %Y}); "
                           f"dated {want:%B %d, %Y} from its position on the page")
            log.info("re-dated a repeated '%s' heading to %s", f"{d:%B %d, %Y}", want)
        else:
            raise FormatError(f"heading {q.heading!r} does not follow {prev:%B %d, %Y}")
        prev = q.quarter_end


def parse_page(page_html: str) -> list[Quarter]:
    """Every quarter on the page, oldest first."""
    html = _COMMENT.sub("", page_html)
    starts = [m.start() for m in _SECTION.finditer(html)]
    if not starts:
        raise FormatError("no 'Cumulative net figures as at the end of' sections found")

    sections = []
    for i, a in enumerate(starts):
        section = html[a:starts[i + 1] if i + 1 < len(starts) else len(html)]
        first = _TABLE.search(section)
        if not first:
            raise FormatError(f"section {i} has no table")
        text = clean(section[:first.start()])
        stated, end = heading_date(text)
        heading = text[:end] if end > 0 else text[:160]
        notes: list[str] = []
        label, net = category_iii(first.group(0), NET_FIGURES, notes, heading)
        q = Quarter(heading=heading, stated_date=stated, net_label=label, net=net, notes=notes)

        m = _EQUITY_DEBT.search(section)
        if m:
            table = _TABLE.search(section, m.end())
            if not table:
                raise FormatError(f"{heading}: equity and debt heading without a table")
            q.equity_debt_label, q.equity_debt = category_iii(
                table.group(0), EQUITY_DEBT, notes, f"{heading} / equity and debt")
            after = _NEXT_HEADING.split(clean(section[table.end():]), 1)[0].strip()
            q.footnote = after if after.startswith("*") else None
        sections.append(q)

    _assign_dates(sections)
    return sections[::-1]
