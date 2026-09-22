"""SQLite-backed state for a long, resumable scrape.

Two ideas keep a 40 000-request job survivable:

* **Every fetched page is checkpointed.**  ``page_log`` records the outcome for
  each (manager, year, month) cell, so an interrupted run resumes exactly where
  it stopped instead of re-requesting what it already has.
* **Raw HTML is archived to disk.**  Parser bugs are discovered late; re-parsing
  an archive is free, whereas re-fetching costs days.  ``pmr reparse`` rebuilds
  every derived row from the archive without touching the network.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS pm (
    pmr_id      TEXT PRIMARY KEY,
    reg_no      TEXT,
    name        TEXT,
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS page_log (
    pmr_id      TEXT NOT NULL,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    status      TEXT NOT NULL,      -- ok | nodata | oldformat | error
    detail      TEXT,
    n_rows      INTEGER DEFAULT 0,
    archive     TEXT,
    sha256      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (pmr_id, year, month)
);

CREATE TABLE IF NOT EXISTS report_meta (
    pmr_id TEXT, year INTEGER, month INTEGER,
    pm_name TEXT, reg_no TEXT, registration_date TEXT,
    PRIMARY KEY (pmr_id, year, month)
);

-- One row per (manager, month, investment approach) for the lettered tables.
CREATE TABLE IF NOT EXISTS b_aum (
    pmr_id TEXT, year INTEGER, month INTEGER,
    investment_approach TEXT, is_total INTEGER, data TEXT,
    PRIMARY KEY (pmr_id, year, month, investment_approach)
);
CREATE TABLE IF NOT EXISTS c_flow (
    pmr_id TEXT, year INTEGER, month INTEGER,
    investment_approach TEXT, is_total INTEGER, net_month REAL,
    PRIMARY KEY (pmr_id, year, month, investment_approach)
);
CREATE TABLE IF NOT EXISTS g_aum (
    pmr_id TEXT, year INTEGER, month INTEGER, data TEXT,
    PRIMARY KEY (pmr_id, year, month)
);
CREATE TABLE IF NOT EXISTS h_flow (
    pmr_id TEXT, year INTEGER, month INTEGER,
    inflow_month REAL, outflow_month REAL, net_month REAL,
    PRIMARY KEY (pmr_id, year, month)
);

CREATE INDEX IF NOT EXISTS ix_page_status ON page_log(status);
CREATE INDEX IF NOT EXISTS ix_b_period   ON b_aum(year, month);
CREATE INDEX IF NOT EXISTS ix_c_period   ON c_flow(year, month);
"""

# The 651-option manager dropdown is ~87 KB of identical markup on every page;
# dropping it before archiving cuts the archive by roughly 40%.
_DROPDOWN = re.compile(r'(<select[^>]*name="pmrId"[^>]*>).*?(</select>)', re.S)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_dropdown(html: str) -> str:
    return _DROPDOWN.sub(r"\1\2", html)


class Store:
    def __init__(self, path: str = "data/pmr.db", archive_dir: str = "data/raw"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.archive_dir = archive_dir
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- catalogue
    def upsert_pms(self, pms) -> int:
        with self.tx() as c:
            c.executemany(
                "INSERT INTO pm(pmr_id,reg_no,name,first_seen) VALUES(?,?,?,?) "
                "ON CONFLICT(pmr_id) DO UPDATE SET reg_no=excluded.reg_no, name=excluded.name",
                [(p.pmr_id, p.reg_no, p.name, _now()) for p in pms])
        return len(pms)

    def pms(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM pm ORDER BY name").fetchall()

    # -------------------------------------------------------------- progress
    def done_cells(self, statuses=("ok", "nodata", "oldformat")) -> set[tuple[str, int, int]]:
        q = "SELECT pmr_id,year,month FROM page_log WHERE status IN (%s)" % ",".join("?" * len(statuses))
        return {(r[0], r[1], r[2]) for r in self.conn.execute(q, statuses)}

    def attempted_cells(self) -> set[tuple[str, int, int]]:
        return {(r[0], r[1], r[2]) for r in self.conn.execute("SELECT pmr_id,year,month FROM page_log")}

    def log_page(self, pmr_id, year, month, status, detail="", n_rows=0, archive=None, sha=None):
        with self.tx() as c:
            c.execute(
                "INSERT INTO page_log(pmr_id,year,month,status,detail,n_rows,archive,sha256,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(pmr_id,year,month) DO UPDATE SET "
                "status=excluded.status, detail=excluded.detail, n_rows=excluded.n_rows, "
                "archive=excluded.archive, sha256=excluded.sha256, fetched_at=excluded.fetched_at",
                (pmr_id, year, month, status, detail[:500], n_rows, archive, sha, _now()))

    # --------------------------------------------------------------- archive
    def archive_path(self, reg_no: str, year: int, month: int) -> str:
        safe = re.sub(r"\W+", "_", reg_no or "unknown")[:40]
        return os.path.join(self.archive_dir, f"{year:04d}-{month:02d}", f"{safe}.html.gz")

    def write_archive(self, reg_no: str, year: int, month: int, html: str) -> tuple[str, str]:
        path = self.archive_path(reg_no, year, month)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = strip_dropdown(html)
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as fh:
            fh.write(body)
        return path, hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def read_archive(path: str) -> str:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    # ----------------------------------------------------------- parsed rows
    def save_report(self, pmr_id: str, rep) -> int:
        n = 0
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO report_meta VALUES(?,?,?,?,?,?)",
                      (pmr_id, rep.year, rep.month, rep.pm_name, rep.pm_reg, rep.registration_date))
            for row in rep.b_aum:
                data = {k: v for k, v in row.items() if k not in ("investment_approach", "is_total")}
                c.execute("INSERT OR REPLACE INTO b_aum VALUES(?,?,?,?,?,?)",
                          (pmr_id, rep.year, rep.month, row["investment_approach"],
                           int(row["is_total"]), json.dumps(data)))
                n += 1
            for row in rep.c_flow:
                c.execute("INSERT OR REPLACE INTO c_flow VALUES(?,?,?,?,?,?)",
                          (pmr_id, rep.year, rep.month, row["investment_approach"],
                           int(row["is_total"]), row["net_inflow_outflow_during_month_inr_cr"]))
                n += 1
            for row in rep.g_aum:
                c.execute("INSERT OR REPLACE INTO g_aum VALUES(?,?,?,?)",
                          (pmr_id, rep.year, rep.month, json.dumps(row)))
                n += 1
            for row in rep.h_flow:
                c.execute("INSERT OR REPLACE INTO h_flow VALUES(?,?,?,?,?,?)",
                          (pmr_id, rep.year, rep.month,
                           row.get("inflow_during_month_inr_cr"),
                           row.get("outflow_during_month_inr_cr"),
                           row.get("net_inflow_outflow_during_month_inr_cr")))
                n += 1
        return n

    # ---------------------------------------------------------------- report
    def coverage(self) -> dict:
        cur = self.conn.execute("SELECT status, COUNT(*) FROM page_log GROUP BY status")
        by_status = {r[0]: r[1] for r in cur}
        counts = {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("b_aum", "c_flow", "g_aum", "h_flow")}
        return {
            "managers": self.conn.execute("SELECT COUNT(*) FROM pm").fetchone()[0],
            "pages_by_status": by_status,
            "rows": counts,
        }


TABLES = ("pm", "page_log", "report_meta", "b_aum", "c_flow", "g_aum", "h_flow")


def merge(inputs: list[str], out_path: str, archive_dir: str = "data/raw") -> dict:
    """Union several shard databases into one.

    Shards write to separate files so parallel workers never contend on a
    single SQLite file; this folds them back together.  ``INSERT OR REPLACE``
    means a later shard's row wins, which is what you want when a cell was
    re-fetched.
    """
    dest = Store(out_path, archive_dir)
    counts = {}
    for i, src in enumerate(inputs):
        if not os.path.exists(src):
            continue
        alias = f"src{i}"
        dest.conn.execute(f"ATTACH DATABASE ? AS {alias}", (src,))
        try:
            with dest.tx() as c:
                for table in TABLES:
                    cur = c.execute(f"INSERT OR REPLACE INTO main.{table} SELECT * FROM {alias}.{table}")
                    counts[table] = counts.get(table, 0) + (cur.rowcount if cur.rowcount > 0 else 0)
        finally:
            dest.conn.execute(f"DETACH DATABASE {alias}")
    dest.conn.commit()
    return counts
