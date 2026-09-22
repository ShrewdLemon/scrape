"""The scrape loop: work planning, shard selection, and graceful shutdown.

A full sweep is roughly 651 managers x 68 months ~ 44 000 requests, which at a
polite 2.5-4.0 s is far longer than any single CI job.  The loop is therefore
built to be stopped and restarted at will: it plans work from what the store
has *not* recorded, honours a wall-clock budget, and flushes cleanly on
SIGTERM so a cancelled job loses nothing.
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import date

from .fetch import PmrClient, PortfolioManager
from .parse import NEW_FORMAT_START, FormatError, parse_report
from .store import Store

log = logging.getLogger(__name__)


def months_between(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) from ``start`` to ``end``."""
    (y0, m0), (y1, m1) = start, end
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def parse_period(text: str) -> tuple[int, int]:
    """``'2021-02'`` -> ``(2021, 2)``."""
    parts = text.replace("/", "-").split("-")
    if len(parts) != 2:
        raise ValueError(f"period must look like YYYY-MM, got {text!r}")
    y, m = int(parts[0]), int(parts[1])
    if not 1 <= m <= 12:
        raise ValueError(f"month out of range in {text!r}")
    return y, m


def default_end() -> tuple[int, int]:
    """Previous month - the current month is not published yet."""
    today = date.today()
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


class Stopper:
    """Flips ``stopped`` on SIGINT/SIGTERM so the loop exits between requests."""

    def __init__(self):
        self.stopped = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):  # not on the main thread
                pass

    def _handle(self, signum, frame):
        if self.stopped:
            raise KeyboardInterrupt
        self.stopped = True
        log.warning("signal %s received - finishing current request then stopping", signum)


@dataclass
class RunStats:
    ok: int = 0
    nodata: int = 0
    oldformat: int = 0
    error: int = 0
    skipped: int = 0
    rows: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def attempted(self) -> int:
        return self.ok + self.nodata + self.oldformat + self.error

    def summary(self) -> str:
        el = time.monotonic() - self.started
        rate = self.attempted / el * 3600 if el else 0
        return (f"{self.attempted} pages in {el/60:.1f} min "
                f"({rate:.0f}/h) - ok={self.ok} nodata={self.nodata} "
                f"legacy={self.oldformat} error={self.error} rows={self.rows}")


def select_shard(pms: list, shard: int, shards: int) -> list:
    """Deterministic contiguous slice, so shards never overlap."""
    if shards <= 1:
        return pms
    if not 0 <= shard < shards:
        raise ValueError(f"shard must be in [0,{shards})")
    return [p for i, p in enumerate(pms) if i % shards == shard]


def plan(store: Store, pms: list, periods: list[tuple[int, int]],
         redo_errors: bool = False) -> list[tuple[PortfolioManager, int, int]]:
    """Cells not yet recorded, ordered manager-major so archives land together."""
    done = store.attempted_cells() if not redo_errors else store.done_cells()
    work = []
    for pm in pms:
        for (y, m) in periods:
            if (pm.pmr_id, y, m) not in done:
                work.append((pm, y, m))
    return work


def classify(html: str, year: int, month: int):
    """Parse a page, mapping the expected failure modes onto a status string."""
    try:
        rep = parse_report(html, year, month)
    except FormatError as exc:
        status = "oldformat" if (year, month) < NEW_FORMAT_START else "nodata"
        return status, None, str(exc)
    if not rep.has_data:
        return "nodata", rep, "; ".join(rep.notes) or "no B/C/G/H rows"
    return "ok", rep, "; ".join(rep.notes)


def run(store: Store, client: PmrClient, pms: list, periods: list[tuple[int, int]],
        limit: int | None = None, max_seconds: float | None = None,
        archive: bool = True, redo_errors: bool = False,
        method: str = "post", progress_every: int = 25) -> RunStats:
    """Fetch, archive and parse every outstanding cell within the budget."""
    stats = RunStats()
    stop = Stopper()
    work = plan(store, pms, periods, redo_errors)
    total = len(work)
    if limit:
        work = work[:limit]
    log.info("planned %d cells (%d outstanding), budget=%s",
             len(work), total, f"{max_seconds}s" if max_seconds else "none")

    for i, (pm, year, month) in enumerate(work, 1):
        if stop.stopped:
            log.warning("stopping early at %d/%d on signal", i, len(work))
            break
        if max_seconds and (time.monotonic() - stats.started) > max_seconds:
            log.warning("wall-clock budget reached at %d/%d", i, len(work))
            break

        try:
            html = client.report_html(pm, year, month, method=method)
        except Exception as exc:
            stats.error += 1
            store.log_page(pm.pmr_id, year, month, "error", f"{type(exc).__name__}: {exc}")
            log.error("fetch failed %s %04d-%02d: %s", pm.reg_no, year, month, exc)
            continue

        status, rep, detail = classify(html, year, month)
        arc = sha = None
        if archive and status != "oldformat":
            try:
                arc, sha = store.write_archive(pm.reg_no, year, month, html)
            except OSError as exc:
                log.error("archive write failed: %s", exc)

        n = store.save_report(pm.pmr_id, rep) if rep is not None else 0
        store.log_page(pm.pmr_id, year, month, status, detail, n, arc, sha)
        stats.rows += n
        setattr(stats, status, getattr(stats, status) + 1)

        if i % progress_every == 0:
            log.info("[%d/%d] %s", i, len(work), stats.summary())

    log.info("run complete: %s", stats.summary())
    return stats


def reparse(store: Store, only_status=("ok", "nodata")) -> RunStats:
    """Rebuild every derived row from the on-disk archive - no network at all."""
    stats = RunStats()
    rows = store.conn.execute(
        "SELECT pmr_id,year,month,archive,sha256 FROM page_log WHERE archive IS NOT NULL "
        "AND status IN (%s)" % ",".join("?" * len(only_status)), only_status).fetchall()
    log.info("reparsing %d archived pages", len(rows))
    for i, r in enumerate(rows, 1):
        try:
            html = store.read_archive(r["archive"])
        except OSError as exc:
            stats.error += 1
            log.error("cannot read %s: %s", r["archive"], exc)
            continue
        status, rep, detail = classify(html, r["year"], r["month"])
        n = store.save_report(r["pmr_id"], rep) if rep is not None else 0
        store.log_page(r["pmr_id"], r["year"], r["month"], status, detail, n,
                       r["archive"], r["sha256"])
        stats.rows += n
        setattr(stats, status, getattr(stats, status) + 1)
        if i % 500 == 0:
            log.info("[%d/%d] reparsed", i, len(rows))
    log.info("reparse complete: %s", stats.summary())
    return stats
