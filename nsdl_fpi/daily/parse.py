"""Pull the daily *Equity* row out of an Archive ("Daily Trends") page.

Two layouts show up between 2001 and 2020:

* **Flat** (2001 - ~2008): each reporting day has an ``Equity`` row and a
  ``Debt`` row, no per-day total.
* **Routed** (~2008 onwards): each category (Equity, Debt, later Hybrid) is
  split into ``Stock Exchange`` / ``Primary market & others`` / ``Sub-total``,
  and every day ends with a ``Total`` row.

The portal's rowspans are not reliable (the last category's rowspan usually
swallows the ``Total`` row), so instead of expanding a grid the parser reads
each row as *labels* (text cells) followed by *values* (the four numbers:
gross purchases, gross sales, net INR crore, net USD million), and tracks
which day / category it is in.

Per day the equity figure is, in order of preference: the Equity
``Sub-total``, the single flat ``Equity`` row, the sum of the Equity route rows
- and only if the day has no equity figures at all, its ``Total`` row (or, in
the flat layout which has no total row, the sum of all categories). The
``basis`` field records which one was used.

In the routed layout the three equity route rows themselves (Stock Exchange,
Primary market & others, Sub-total) are kept too, as ``DayEquity.routes``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from ..parse import FormatError, MONTHS, _Rows, to_number

VALUE_COLS = ("gross_purchases", "gross_sales", "net_inr", "net_usd")
ROUTE_NAMES = ("Stock Exchange", "Primary market & others", "Sub-total")
ROUTES = {r.lower() for r in ROUTE_NAMES}

_DATE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")
_TITLE = re.compile(r"Daily Trends in FPI Investments\s+up\s*to\s+(\d{2}-[A-Za-z]{3}-\d{4})", re.I)
_FX = re.compile(r"Rs\.?\s*([\d.]+)")
_BLANK = re.compile(r"^(?:[-–—]*|N\.?A\.?|nil)$", re.I)

BASIS_SUBTOTAL = "Equity sub-total"
BASIS_EQUITY = "Equity"
BASIS_ROUTES = "Equity routes (summed)"
BASIS_TOTAL = "Total row"
BASIS_TOTAL_SUM = "Total (all categories summed)"
EQUITY_BASES = (BASIS_SUBTOTAL, BASIS_EQUITY, BASIS_ROUTES)


def _num(text: str):
    """(is_value, number) - blanks/dashes count as a value cell holding None."""
    if _BLANK.match(text.strip()):
        return True, None
    try:
        return True, to_number(text)
    except FormatError:
        return False, None


def _d(text: str) -> date:
    return datetime.strptime(text, "%d-%b-%Y").date()


@dataclass
class Entry:
    category: str | None      # Equity / Debt / Hybrid ... (None for a Total row)
    route: str | None         # Stock Exchange / Primary market & others / Sub-total / None
    values: tuple             # VALUE_COLS, None where blank
    is_total: bool = False


@dataclass
class Block:
    label: str                # a date string, "Total for January", "Total for 2010", "Grand Total ..."
    fx: float | None = None
    entries: list[Entry] = field(default_factory=list)

    def equity_routes(self) -> dict[str, tuple]:
        """{"Stock Exchange" | "Primary market & others" | "Sub-total": values} for Equity."""
        canon = {r.lower(): r for r in ROUTE_NAMES}
        return {canon[e.route.lower()]: e.values for e in self.entries
                if not e.is_total and (e.category or "").lower() == "equity" and e.route}

    def pick(self) -> tuple[str, tuple] | None:
        """(basis, values) per the preference order in the module docstring."""
        def ok(e):
            return e.values and e.values[2] is not None
        eq = [e for e in self.entries if not e.is_total and (e.category or "").lower() == "equity"]
        for e in eq:
            if (e.route or "").lower() == "sub-total" and ok(e):
                return BASIS_SUBTOTAL, e.values
        for e in eq:
            if e.route is None and ok(e):
                return BASIS_EQUITY, e.values
        routes = [e for e in eq if ok(e)]
        if routes:
            return BASIS_ROUTES, _sum(e.values for e in routes)
        for e in self.entries:
            if e.is_total and ok(e):
                return BASIS_TOTAL, e.values
        cats = {}
        for e in self.entries:          # flat layout: one row per category
            if not e.is_total and ok(e) and (e.route is None or e.route.lower() == "sub-total"):
                cats[e.category] = e.values
        if cats:
            return BASIS_TOTAL_SUM, _sum(cats.values())
        return None


def _sum(rows) -> tuple:
    rows = list(rows)
    out = []
    for i in range(len(VALUE_COLS)):
        vals = [r[i] for r in rows if r[i] is not None]
        out.append(round(sum(vals), 2) if vals else None)
    return tuple(out)


@dataclass
class DayEquity:
    day: date
    basis: str
    gross_purchases: float | None
    gross_sales: float | None
    net_inr: float | None
    net_usd: float | None
    fx: float | None
    routes: dict[str, tuple] = field(default_factory=dict)   # routed layout only


@dataclass
class MonthReport:
    year: int
    month: int
    upto: date
    layout: str                               # "flat" / "routed"
    days: list[DayEquity]
    stated: tuple | None                      # "Total for <Month>" equity values
    stated_basis: str | None
    categories: list[str]
    stated_routes: dict[str, tuple] = field(default_factory=dict)   # month total, per equity route

    @property
    def computed_net_inr(self) -> float:
        return round(sum(d.net_inr or 0 for d in self.days if d.basis in EQUITY_BASES), 2)

    @property
    def computed_net_usd(self) -> float:
        return round(sum(d.net_usd or 0 for d in self.days if d.basis in EQUITY_BASES), 2)

    def route_sum(self, route: str, col: int = 2) -> float:
        """Sum of one equity route over the month (``col`` indexes VALUE_COLS)."""
        return round(sum(d.routes[route][col] or 0 for d in self.days if route in d.routes), 2)

    @property
    def fallback_days(self) -> list[DayEquity]:
        return [d for d in self.days if d.basis not in EQUITY_BASES]


def _blocks(rows) -> tuple[list[Block], set[str]]:
    blocks: list[Block] = []
    cats: set[str] = set()
    cur: Block | None = None
    category: str | None = None
    for row in rows:
        if not row or row[0][0] == "th":
            continue
        texts = [t for _, t in row]
        labels, values, fx = [], [], None
        for t in texts:
            m = _FX.fullmatch(t.replace(" ", "")) or _FX.fullmatch(t)
            if m:
                fx = float(m.group(1))
                continue
            is_value, n = _num(t)
            if is_value:
                values.append(n)
            elif not values:
                labels.append(t)
        if labels and (_DATE.match(labels[0]) or labels[0].lower().startswith(("total for", "grand total"))):
            cur = Block(labels.pop(0))
            blocks.append(cur)
            category = None
        if cur is None:
            continue
        if fx is not None and cur.fx is None:
            cur.fx = fx
        if len(values) < len(VALUE_COLS):
            continue                     # notes / footers
        is_total, route = False, None
        for lab in labels:
            low = lab.lower()
            if low == "total":
                is_total = True
            elif low in ROUTES:
                route = lab
            else:
                category = lab
                cats.add(lab)
        cur.entries.append(Entry(None if is_total else category, None if is_total else route,
                                 tuple(values[:len(VALUE_COLS)]), is_total))
    return blocks, cats


def parse_month(html: str, year: int, month: int) -> MonthReport:
    t = _Rows()
    t.feed(html)
    if not t.rows:
        raise FormatError(f"{year}-{month:02d}: no tbls01 table on the page")
    head = " ".join(txt for r in t.rows[:3] for _, txt in r)
    m = _TITLE.search(head)
    if not m:
        raise FormatError(f"{year}-{month:02d}: report title not found")
    upto = _d(m.group(1))
    if (upto.year, upto.month) != (year, month):
        raise FormatError(f"asked for {year}-{month:02d}, page is up to {upto}")
    hdr = " ".join(txt for r in t.rows[:4] for tag, txt in r if tag == "th").lower()
    if "net investment" not in hdr or "gross purchases" not in hdr:
        raise FormatError(f"{year}-{month:02d}: unexpected column headers")

    blocks, cats = _blocks(t.rows)
    days, stated, stated_basis, stated_routes = [], None, None, {}
    month_label = f"total for {MONTHS[month - 1].lower()}"
    for b in blocks:
        if _DATE.match(b.label):
            d = _d(b.label)
            if (d.year, d.month) != (year, month):
                raise FormatError(f"{year}-{month:02d}: stray reporting date {d}")
            got = b.pick()
            if got is None:
                raise FormatError(f"{d}: neither an equity nor a total row")
            basis, v = got
            days.append(DayEquity(d, basis, *v, b.fx, b.equity_routes()))
        elif b.label.lower() == month_label and stated is None:
            got = b.pick()
            if got:
                stated_basis, stated = got
                stated_routes = b.equity_routes()
    if not days:
        raise FormatError(f"{year}-{month:02d}: no reporting days found")
    seen = [d.day for d in days]
    if len(seen) != len(set(seen)):
        raise FormatError(f"{year}-{month:02d}: duplicate reporting dates")
    routed = any(e.route for b in blocks for e in b.entries)
    return MonthReport(year, month, upto, "routed" if routed else "flat", days,
                       stated, stated_basis, sorted(cats), stated_routes)
