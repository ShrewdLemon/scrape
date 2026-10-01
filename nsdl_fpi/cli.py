"""Command line: ``python -m nsdl_fpi [--since 2002] [-o output/fpi_equity.xlsx]``."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date

from .excel import long_rows, mismatches, write_csv, write_workbook
from .fetch import CURRENCIES, NsdlClient, available_years
from .parse import parse_year

log = logging.getLogger("nsdl_fpi")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nsdl_fpi", description=__doc__)
    p.add_argument("--since", type=int, default=2002, help="first calendar year (default 2002)")
    p.add_argument("--until", type=int, default=None, help="last calendar year (default: latest on the site)")
    p.add_argument("--currency", nargs="+", choices=CURRENCIES, default=list(CURRENCIES))
    p.add_argument("-o", "--output", default="output/nsdl_fpi_equity.xlsx")
    p.add_argument("--csv", default="output/nsdl_fpi_equity.csv", help="'' to skip")
    p.add_argument("--raw-dir", default="output/raw",
                   help="keep each fetched page here ('' to skip)")
    p.add_argument("--offline", action="store_true",
                   help="rebuild from --raw-dir only, no network")
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.0)
    p.add_argument("--strict", action="store_true",
                   help="exit non-zero if any year fails to reconcile with NSDL's total")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    t0 = time.monotonic()
    client = None if a.offline else NsdlClient(a.min_delay, a.max_delay)

    if a.offline:
        years = range(a.since, (a.until or date.today().year) + 1)
    else:
        site_years = available_years(client.landing())
        years = [y for y in site_years if a.since <= y <= (a.until or site_years[-1])]
    log.info("years %s-%s, currencies %s", min(years), max(years), " ".join(a.currency))

    reports = []
    for cur in a.currency:
        for y in years:
            raw = os.path.join(a.raw_dir, f"{y}_{cur}.html") if a.raw_dir else None
            if a.offline:
                if not raw or not os.path.exists(raw):
                    log.warning("no archived page for %s %s, skipping", y, cur)
                    continue
                with open(raw, encoding="utf-8") as fh:
                    html = fh.read()
            else:
                html = client.year(y, cur)
                if raw:
                    os.makedirs(a.raw_dir, exist_ok=True)
                    with open(raw, "w", encoding="utf-8") as fh:
                        fh.write(html)
            rep = parse_year(html, y, cur)
            reports.append(rep)
            log.info("%s %s: %2d months, total %s", y, cur, len(rep.equity), rep.stated_total)

    if not reports:
        log.error("nothing scraped")
        return 1
    write_workbook(a.output, reports, a.currency)
    log.info("wrote %s", a.output)
    if a.csv:
        write_csv(a.csv, long_rows(reports, a.currency))
        log.info("wrote %s", a.csv)

    bad = mismatches(reports)
    for r in bad:
        log.warning("%s %s does not reconcile: sum %s vs NSDL %s",
                    r.year, r.currency, r.computed_total, r.stated_total)
    log.info("done in %.0fs, %d pages, %d reconciliation issues",
             time.monotonic() - t0, len(reports), len(bad))
    return 2 if (bad and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
