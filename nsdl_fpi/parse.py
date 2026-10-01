"""Extract the FPI *Equity* column from a year-wise report page.

The table (``class='tbls01'``) has a different shape per era - 2002 has just
Equity / Debt / Total, while recent years add Debt-VRR, Debt-FAR, Hybrid,
Mutual Funds (which has its own "Equity" sub-column!) and AIFs. In every era
the *first* leaf header is the FPI Equity column, so the parser anchors on
that and refuses the page if it ever stops being true, rather than silently
picking up a different column.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
UNITS = {"INR": "INR Crores", "USD": "USD Million"}


class FormatError(ValueError):
    """The page does not look like the report we know how to read."""


class _Rows(HTMLParser):
    """Collects (tag, text) cells per <tr> of the first tbls01 table.

    The portal omits many ``</tr>`` tags, so a new ``<tr>`` closes the previous
    row implicitly.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str]]] = []
        self._depth = 0          # nesting depth inside the target table
        self._done = False
        self._cell: list[str] | None = None
        self._tag = ""

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "table":
            if self._depth or "tbls01" in (dict(attrs).get("class") or ""):
                self._depth += 1
        elif self._depth == 1 and tag == "tr":
            self._close_cell()
            self.rows.append([])
        elif self._depth == 1 and tag in ("td", "th") and self.rows:
            self._close_cell()
            self._cell, self._tag = [], tag

    def handle_endtag(self, tag):
        if self._done or not self._depth:
            return
        if tag in ("td", "th") and self._depth == 1:
            self._close_cell()
        elif tag == "table":
            self._close_cell()
            self._depth -= 1
            self._done = self._depth == 0

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self):
        if self._cell is not None and self.rows:
            self.rows[-1].append((self._tag, " ".join("".join(self._cell).split())))
        self._cell = None


def to_number(text: str) -> float | int | None:
    t = unescape(text).replace(",", "").replace("−", "-").strip()
    if t in ("", "-", "--", "NA", "N.A."):
        return None
    if re.fullmatch(r"\(\s*[\d.]+\s*\)", t):      # accounting negatives
        t = "-" + t.strip("() ")
    try:
        v = float(t)
    except ValueError:
        raise FormatError(f"not a number: {text!r}") from None
    return int(v) if v.is_integer() else v


@dataclass
class YearReport:
    year: int
    currency: str
    equity: dict[int, float | int | None]       # month number -> value
    stated_total: float | int | None             # NSDL's own "Total - YYYY" row
    leaf_headers: list[str] = field(default_factory=list)

    @property
    def computed_total(self):
        vals = [v for v in self.equity.values() if v is not None]
        return sum(vals) if vals else None


def parse_year(html: str, year: int, currency: str) -> YearReport:
    p = _Rows()
    p.feed(html)
    rows = [r for r in p.rows if r]
    if not rows:
        raise FormatError("report table (tbls01) not found")

    title = " ".join(t for _, t in rows[0])
    m = re.search(r"Calendar Year\s*-\s*(\d{4})", title)
    if not m or int(m.group(1)) != year:
        raise FormatError(f"asked for {year}, page shows {title!r}")

    header_rows = [r for r in rows if all(tag == "th" for tag, _ in r)]
    unit = UNITS[currency]
    if not any(unit in t for r in header_rows for _, t in r):
        raise FormatError(f"expected unit {unit!r} in headers")

    # The leaf header row is the last all-<th> row before the first month row.
    first_data = next((i for i, r in enumerate(rows) if r[0][1] in MONTHS), None)
    if first_data is None:
        raise FormatError("no month rows")
    leaf = [t for _, t in rows[first_data - 1]]
    if not leaf or leaf[0] != "Equity":
        raise FormatError(f"first leaf column is {leaf[:1]}, expected 'Equity'")

    equity: dict[int, float | int | None] = {}
    stated = None
    for r in rows[first_data:]:
        label = r[0][1]
        if label in MONTHS and len(r) > 1:
            equity[MONTHS.index(label) + 1] = to_number(r[1][1])
        elif label.startswith("Total") and len(r) > 1:
            stated = to_number(r[1][1])
    return YearReport(year, currency, equity, stated, leaf)
