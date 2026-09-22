"""Per-month filing roster, built from the portal's consolidated export.

The export at ``doPmrExcel=yes`` ignores ``pmrId`` and returns one consolidated
sheet covering every manager that filed for that month - 415 of the 651
registered managers in March 2024.  It is one row per manager, so it cannot
carry tables B and C (which are per investment approach), but it answers a
cheaper question exactly: *who filed this month?*

One request per month therefore removes every manager-month that has nothing
to fetch, which is where most of the wasted requests in a historical sweep go.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

REG_RE = re.compile(r"^INP\d{9}$")


class RosterUnavailable(RuntimeError):
    """The export could not be read (missing xlrd, bad payload, empty month)."""


def parse_consolidated(body: bytes) -> list[dict]:
    """Rows of ``{reg_no, name, status, month_year}`` from the consolidated .xls."""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RosterUnavailable(
            "reading the consolidated export needs xlrd (pip install xlrd)") from exc
    try:
        book = xlrd.open_workbook(file_contents=body)
    except Exception as exc:
        raise RosterUnavailable(f"could not open export: {type(exc).__name__}: {exc}") from exc

    out: list[dict] = []
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            try:
                row = [str(sheet.cell_value(r, c)).strip() for c in range(min(sheet.ncols, 5))]
            except IndexError:
                continue
            reg = next((v for v in row if REG_RE.match(v)), None)
            if not reg:
                continue
            out.append({
                "reg_no": reg,
                "name": row[0] if row else "",
                "status": row[1] if len(row) > 1 else "",
                "month_year": row[3] if len(row) > 3 else "",
            })
    if not out:
        raise RosterUnavailable("no registration numbers found in the export")
    return out


ROSTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS filing_roster (
    year INTEGER, month INTEGER, reg_no TEXT, name TEXT, status TEXT,
    PRIMARY KEY (year, month, reg_no)
);
CREATE TABLE IF NOT EXISTS roster_log (
    year INTEGER, month INTEGER, filers INTEGER, fetched_at TEXT,
    PRIMARY KEY (year, month)
);
"""


def save_roster(store, year: int, month: int, rows: list[dict]) -> int:
    from .store import _now

    store.conn.executescript(ROSTER_SCHEMA)
    with store.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO filing_roster(year,month,reg_no,name,status) VALUES(?,?,?,?,?)",
            [(year, month, r["reg_no"], r["name"], r["status"]) for r in rows])
        c.execute("INSERT OR REPLACE INTO roster_log VALUES(?,?,?,?)",
                  (year, month, len(rows), _now()))
    return len(rows)


def filers(store, year: int, month: int) -> set[str] | None:
    """Registration numbers that filed for this month, or ``None`` if unknown."""
    store.conn.executescript(ROSTER_SCHEMA)
    known = store.conn.execute(
        "SELECT filers FROM roster_log WHERE year=? AND month=?", (year, month)).fetchone()
    if not known:
        return None
    return {r[0] for r in store.conn.execute(
        "SELECT reg_no FROM filing_roster WHERE year=? AND month=?", (year, month))}


def build(store, client, periods, save_dir: str | None = None) -> dict:
    """Download one consolidated export per month and record who filed."""
    summary = {"months": 0, "filers": 0, "failed": []}
    for (year, month) in periods:
        try:
            body, ctype, status = client.export_bytes(None, year, month,
                                                      fmt="excel", method="post")
        except Exception as exc:
            log.warning("roster %04d-%02d fetch failed: %s", year, month, exc)
            summary["failed"].append(f"{year}-{month:02d}: {type(exc).__name__}")
            continue
        if save_dir:
            import os
            os.makedirs(save_dir, exist_ok=True)
            with open(os.path.join(save_dir, f"consolidated_{year}{month:02d}.xls"), "wb") as fh:
                fh.write(body)
        try:
            rows = parse_consolidated(body)
        except RosterUnavailable as exc:
            log.warning("roster %04d-%02d unusable: %s", year, month, exc)
            summary["failed"].append(f"{year}-{month:02d}: {exc}")
            continue
        n = save_roster(store, year, month, rows)
        summary["months"] += 1
        summary["filers"] += n
        log.info("roster %04d-%02d: %d managers filed", year, month, n)
    return summary
