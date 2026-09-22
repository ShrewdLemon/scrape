"""Parse a SEBI PMR report page into the four tables we care about.

Target tables (SEBI's own lettering, new-format reports only):

==========  ========================================================  ===============================
Section     Table                                                     What we keep
==========  ========================================================  ===============================
B           Break-up of AUM of the Portfolio Manager (Discretionary)  every AUM column
C           Funds Inflow/Outflow (Discretionary)                      Net Inflow/Outflow during month
G           Break-up of AUM of the Portfolio Manager (Non-Disc.)      every AUM column
H           Funds Inflow/Outflow (Non-Discretionary)                  the "During the Month" group
==========  ========================================================  ===============================

Tables are located by their ``<h3>`` heading letter rather than by position,
because the number of sections on a page varies with which services a manager
offers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tables import _TABLE, clean, expand_with_tags, header_depth, to_number

_H3 = re.compile(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]\s*>", re.I | re.S)
# "B. Break-up ...", "M.<tab><u>Break-up ...", "H. <u>Funds Inflow/ Outflow</u>"
_LETTERED = re.compile(r"^([A-Z])[.)]\s*(.+)$")

#: First month SEBI published the lettered (investment-approach) PMR format.
NEW_FORMAT_START = (2021, 2)


class FormatError(ValueError):
    """Page did not contain the lettered new-format sections."""


@dataclass
class Report:
    pm_name: str = ""
    pm_reg: str = ""
    registration_date: str = ""
    year: int = 0
    month: int = 0
    b_aum: list[dict] = field(default_factory=list)
    c_flow: list[dict] = field(default_factory=list)
    g_aum: list[dict] = field(default_factory=list)
    h_flow: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.b_aum or self.c_flow or self.g_aum or self.h_flow)


def _headings(html: str) -> list[tuple[str, str, int]]:
    """``(letter, title, end_offset)`` for every lettered heading on the page."""
    out = []
    for m in _H3.finditer(html):
        text = clean(m.group(1))
        lm = _LETTERED.match(text)
        if lm:
            out.append((lm.group(1), lm.group(2).strip(), m.end()))
    return out


def _table_after(html: str, offset: int) -> str | None:
    m = _TABLE.search(html, offset)
    return m.group(0) if m else None


def find_table(html: str, letter: str, title_contains: str) -> str | None:
    """First ``<table>`` following the ``<h3>`` for ``letter``."""
    want = title_contains.lower()
    for ltr, title, end in _headings(html):
        if ltr == letter and want in title.lower():
            return _table_after(html, end)
    return None


def _column_names(grid: list[list[str]], depth: int) -> list[str]:
    """Canonical names from the two most specific header rows.

    ``Equity`` over ``Listed`` becomes ``Equity - Listed``; a column whose
    group and sub-header are identical (a ``rowspan`` such as ``Total``) stays
    as the single name.
    """
    if depth == 0:
        return [f"col{i}" for i in range(len(grid[0]) if grid else 0)]
    group_row = grid[depth - 2] if depth >= 2 else grid[depth - 1]
    sub_row = grid[depth - 1]
    names = []
    for i in range(len(sub_row)):
        group = (group_row[i] if i < len(group_row) else "").strip()
        sub = (sub_row[i] if i < len(sub_row) else "").strip()
        if not sub or sub == group:
            names.append(group or sub or f"col{i}")
        elif not group or len(group) > 60:  # the wide "Assets under Management..." banner
            names.append(sub)
        else:
            names.append(f"{group} - {sub}")
    return names


def _general_info(html: str) -> tuple[str, str, str]:
    """Name / registration number / registration date from the first table."""
    m = _TABLE.search(html)
    name = reg = date = ""
    while m:
        grid, _ = expand_with_tags(m.group(0))
        for row in grid:
            if len(row) < 2:
                continue
            key = row[0].lower()
            if "name of the portfolio manager" in key and not name:
                name = row[1].strip()
            elif "registration number" in key and not reg:
                reg = row[1].strip()
            elif "date of registration" in key and not date:
                date = row[1].strip()
        if name and reg:
            break
        m = _TABLE.search(html, m.end())
    return name, reg, date


def _is_total(label: str) -> bool:
    return label.strip().lower().rstrip(":").rstrip("*").strip() in {"total", "grand total"}


def _parse_aum(table_html: str) -> tuple[list[str], list[list[str]]]:
    grid, tags = expand_with_tags(table_html)
    if not grid:
        return [], []
    depth = header_depth(grid, tags)
    cols = _column_names(grid, depth)
    return cols, grid[depth:]


def parse_report(html: str, year: int, month: int) -> Report:
    """Extract tables B, C, G and H from one PMR page."""
    rep = Report(year=year, month=month)
    rep.pm_name, rep.pm_reg, rep.registration_date = _general_info(html)

    letters = {ltr for ltr, _, _ in _headings(html)}
    if not {"B", "C"} & letters:
        raise FormatError(
            "no lettered B/C sections on page "
            f"(headings found: {','.join(sorted(letters)) or 'none'})"
        )

    # ---- B: discretionary AUM broken up by investment approach -------------
    tb = find_table(html, "B", "break-up of assets under management")
    if tb:
        cols, rows = _parse_aum(tb)
        for row in rows:
            approach = row[0].strip()
            if not approach:
                continue
            rec = {"investment_approach": approach, "is_total": _is_total(approach)}
            for i in range(1, len(cols)):
                rec[cols[i]] = to_number(row[i]) if i < len(row) else None
            rep.b_aum.append(rec)
    else:
        rep.notes.append("table B missing")

    # ---- C: discretionary flows, only the net-during-month column ----------
    tc = find_table(html, "C", "funds inflow")
    if tc:
        grid, tags = expand_with_tags(tc)
        depth = header_depth(grid, tags)
        cols = _column_names(grid, depth)
        idx = _net_month_index(cols)
        if idx is None:
            rep.notes.append(f"table C: net-month column not found in {cols}")
        else:
            for row in grid[depth:]:
                approach = row[0].strip()
                if not approach:
                    continue
                rep.c_flow.append({
                    "investment_approach": approach,
                    "is_total": _is_total(approach),
                    "net_inflow_outflow_during_month_inr_cr": (
                        to_number(row[idx]) if idx < len(row) else None),
                })
    else:
        rep.notes.append("table C missing")

    # ---- G: non-discretionary AUM (a single unlabelled row) ----------------
    tg = find_table(html, "G", "break-up of assets under management")
    if tg:
        cols, rows = _parse_aum(tg)
        for row in rows:
            rec = {cols[i]: (to_number(row[i]) if i < len(row) else None)
                   for i in range(len(cols))}
            if any(v is not None for v in rec.values()):
                rep.g_aum.append(rec)
    else:
        rep.notes.append("table G missing")

    # ---- H: non-discretionary flows, only the during-the-month group -------
    th = find_table(html, "H", "funds inflow")
    if th:
        grid, tags = expand_with_tags(th)
        depth = header_depth(grid, tags)
        if depth >= 2:
            groups, subs = grid[depth - 2], grid[depth - 1]
            keep = [i for i in range(len(subs))
                    if "month" in groups[i].lower() and "fy" not in groups[i].lower()]
            for row in grid[depth:]:
                rec = {}
                for i in keep:
                    rec[_short_flow_name(subs[i])] = to_number(row[i]) if i < len(row) else None
                if any(v is not None for v in rec.values()):
                    rep.h_flow.append(rec)
            if not keep:
                rep.notes.append(f"table H: no 'during the month' group in {groups}")
    else:
        rep.notes.append("table H missing")

    return rep


def _net_month_index(cols: list[str]) -> int | None:
    """Index of 'Net Inflow (+ve)/ Outflow (-ve) during the month'.

    Must match the *month* column, never the visually similar FY one.
    """
    for i, c in enumerate(cols):
        low = c.lower()
        if "net inflow" in low and "during the month" in low and "fy" not in low:
            return i
    return None


def _short_flow_name(sub: str) -> str:
    low = sub.lower()
    if low.startswith("net"):
        return "net_inflow_outflow_during_month_inr_cr"
    if low.startswith("inflow"):
        return "inflow_during_month_inr_cr"
    if low.startswith("outflow"):
        return "outflow_during_month_inr_cr"
    return re.sub(r"[^a-z0-9]+", "_", low).strip("_")
