"""``python -m apmi_insights`` - scrape one tab/period of the table to Excel."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import PERIODS, TABS
from .excel import write_workbook
from .fetch import ApmiClient, page_url

IST = timezone(timedelta(hours=5, minutes=30))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="apmi_insights", description=__doc__)
    p.add_argument("--tab", type=int, default=0, choices=sorted(TABS),
                   help="0 Equity, 1 Debt, 2 Hybrid, 3 Multi-Asset (the URL's /N)")
    p.add_argument("--period", default="SI", choices=["SI"])
    p.add_argument("--page-size", type=int, default=10, help="rows per page (site uses 10)")
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--max-delay", type=float, default=2.0)
    p.add_argument("--out", default=None, help="output .xlsx path")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    tab = TABS[a.tab]
    started = datetime.now(timezone.utc)
    client = ApmiClient(a.tab, a.min_delay, a.max_delay)
    client.discover()
    rows, pages = client.all_pages(a.page_size, a.period)
    total = client.total(a.period)
    ids = [r.get("pmsIaId") for r in rows]
    dupes = len(ids) - len(set(ids))
    ok = len(rows) == total and dupes == 0

    out = a.out or os.path.join(
        "output", f"apmi_{tab.lower()}_{a.period.lower()}_{started:%Y%m%d}.xlsx")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    meta = [
        ("Source", page_url(a.tab)),
        ("Asset class tab", f"{tab} (/{a.tab})"),
        ("Period view", f"{a.period} - Since Inception"),
        ("Filters", "None (Age, AUM and Service Type all 'No Filter')"),
        ("Scraped at (UTC)", started.strftime("%Y-%m-%d %H:%M:%S")),
        ("Scraped at (IST)", started.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")),
        ("Page size", a.page_size),
        ("Pages with rows", pages),
        ("Rows scraped", len(rows)),
        ("Site total (single full-size request)", total),
        ("Duplicate approach ids", dupes),
        ("Row count check", "OK - matches site total" if ok else "MISMATCH - investigate"),
        ("HTTP requests", client.requests_made),
        ("Server action id", client.action_id),
        ("Row order", "As served by the site (IA since-inception return, highest first)"),
        ("Notes",
         "The site shows no total, so the full-size request is the reference count. "
         "'IA Details' is split into approach and manager sub-columns. "
         "Returns are % values shown to 1 decimal like the site, stored at full "
         "precision. A missing return would be left blank; the site shows 0.0%. "
         "'Actions' links to the approach's View Details report."),
    ]
    write_workbook(out, rows, meta, a.period, title=f"{tab} {a.period}")
    print(f"{len(rows)} rows over {pages} pages (site total {total}) -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
