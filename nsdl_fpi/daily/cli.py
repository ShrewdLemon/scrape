"""Daily FPI equity from NSDL's Archive report.

``python -m nsdl_fpi.daily [--start 2001-01] [--end 2020-01] [-o output/...xlsx]``
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .excel import (ROUTE_COLS, check, check_routes, daily_rows, mismatches, route_rows,
                    write_csv, write_workbook)
from .fetch import ArchiveClient, months
from .parse import parse_month
from .route_workbook import write_route_workbook

log = logging.getLogger("nsdl_fpi.daily")


def _ym(text: str) -> tuple[int, int]:
    y, m = text.split("-")
    if not 1 <= int(m) <= 12:
        raise argparse.ArgumentTypeError(f"bad month in {text!r}")
    return int(y), int(m)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nsdl_fpi.daily", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=_ym, default=(2001, 1), help="first month, YYYY-MM (default 2001-01)")
    p.add_argument("--end", type=_ym, default=(2020, 1), help="last month, YYYY-MM (default 2020-01)")
    p.add_argument("-o", "--output", default="output/nsdl_fpi_daily_equity.xlsx")
    p.add_argument("--csv", default="output/nsdl_fpi_daily_equity.csv", help="'' to skip")
    p.add_argument("--route-workbook", default="output/nsdl_fpi_equity_by_route.xlsx",
                   help="four-sheet workbook: Total / Stock Exchange / Primary / Combined (formulas); '' to skip")
    p.add_argument("--routes-csv", default="output/nsdl_fpi_daily_equity_routes.csv",
                   help="equity split by investment route ('' to skip)")
    p.add_argument("--raw-dir", default="output/raw_daily",
                   help="keep each fetched month page here ('' to skip); reused on reruns")
    p.add_argument("--offline", action="store_true", help="rebuild from --raw-dir only, no network")
    p.add_argument("--refetch", action="store_true", help="ignore pages already in --raw-dir")
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.0)
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if any month fails to reconcile with NSDL's month total")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    span = months(a.start, a.end)
    if not span:
        log.error("--start is after --end")
        return 1
    log.info("%d months, %d-%02d to %d-%02d", len(span), *span[0], *span[-1])
    t0 = time.monotonic()
    client, fetched, reports = None, 0, []

    for i, (y, m) in enumerate(span, 1):
        raw = os.path.join(a.raw_dir, f"{y}-{m:02d}.html") if a.raw_dir else None
        if raw and os.path.exists(raw) and (a.offline or not a.refetch):
            with open(raw, encoding="utf-8") as fh:
                html = fh.read()
        elif a.offline:
            log.warning("no archived page for %d-%02d, skipping", y, m)
            continue
        else:
            client = client or ArchiveClient(a.min_delay, a.max_delay)
            html = client.month(y, m)
            fetched += 1
            if raw:
                os.makedirs(a.raw_dir, exist_ok=True)
                with open(raw, "w", encoding="utf-8") as fh:
                    fh.write(html)
        rep = parse_month(html, y, m)
        reports.append(rep)
        di, _, ok = check(rep)
        eta = (time.monotonic() - t0) / i * (len(span) - i)
        log.info("[%3d/%d] %d-%02d %-6s %2d days, equity %12.2f cr%s%s  (eta %.0fs)",
                 i, len(span), y, m, rep.layout, len(rep.days), rep.computed_net_inr,
                 "" if ok else f"  diff {di}", f"  {len(rep.fallback_days)} fallback day(s)"
                 if rep.fallback_days else "", eta)

    if not reports:
        log.error("nothing scraped")
        return 1
    write_workbook(a.output, reports)
    log.info("wrote %s", a.output)
    if a.csv:
        write_csv(a.csv, daily_rows(reports))
        log.info("wrote %s", a.csv)
    if a.route_workbook:
        write_route_workbook(a.route_workbook, reports)
        log.info("wrote %s", a.route_workbook)
    if a.routes_csv:
        write_csv(a.routes_csv, route_rows(reports), ROUTE_COLS)
        log.info("wrote %s", a.routes_csv)

    bad = mismatches(reports)
    for r in bad:
        log.warning("%d-%02d does not reconcile: daily sum %s vs NSDL %s; worst route diff %s",
                    r.year, r.month, r.computed_net_inr, r.stated and r.stated[2], check_routes(r)[0])
    days = sum(len(r.days) for r in reports)
    fb = sum(len(r.fallback_days) for r in reports)
    log.info("done in %.0fs: %d months (%d fetched), %d days, %d on Total-row fallback, "
             "%d reconciliation issues", time.monotonic() - t0, len(reports), fetched, days, fb, len(bad))
    return 2 if (bad and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
