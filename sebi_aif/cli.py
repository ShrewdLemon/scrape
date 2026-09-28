"""Command line entry point: ``python -m sebi_aif``.

One request fetches the whole page - every quarter since September 2012 - so
each run rebuilds every output from scratch; there is nothing to resume.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import os
from datetime import datetime, timezone

from .excel import UNIT, WORKBOOK, build_tables, write_outputs
from .fetch import fetch_page
from .parse import URL, parse_page


def _read(path: str) -> str:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def _archive(html: str, directory: str) -> str:
    """Keep the fetched page: SEBI edits it in place, so each copy is a snapshot."""
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(directory, f"aif_statistics_{stamp}.html.gz")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(html)
    return path


def _fmt(v) -> str:
    return "-" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".")


def _summary(quarters, counts, out_dir: str) -> None:
    first, last = quarters[0], quarters[-1]
    eq = [q for q in quarters if q.equity_debt is not None]
    print(f"Category III AIF rows for {len(quarters)} quarters, "
          f"{first.quarter_end:%b %Y} to {last.quarter_end:%b %Y}")
    for name, n in counts.items():
        print(f"  {name:<18} {n:>3} rows")
    if eq:
        print(f"  (equity and debt table published from {eq[0].quarter_end:%b %Y})")
    redated = [q for q in quarters if q.stated_date != q.quarter_end]
    if redated:
        print(f"Re-dated {len(redated)} repeated headings: "
              + ", ".join(f"{q.quarter_end:%b %Y}" for q in redated))
    head, rows = build_tables([last])["CatIII_Combined"]
    print(f"Latest quarter ({last.quarter_end:%b %Y}), Rs crore:")
    for title, v in zip(head, rows[0]):
        if title.endswith(UNIT):
            print(f"  {title[:-len(UNIT)]:<52} {_fmt(v):>12}")
    print(f"wrote {os.path.join(out_dir, WORKBOOK)} and {len(counts)} CSVs in "
          f"{os.path.join(out_dir, 'csv')}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sebi_aif",
        description="Scrape the Category III AIF rows of SEBI's AIF statistics page "
                    "(net figures + equity and debt tables) for every quarter.")
    p.add_argument("--html", help="parse a saved copy of the page (.html or .html.gz) "
                                  "instead of fetching it")
    p.add_argument("--out-dir", default="output/aif", help="where the workbook and CSVs go")
    p.add_argument("--archive-dir", default="data/aif/raw", help="where fetched pages are kept")
    p.add_argument("--no-archive", action="store_true", help="do not keep the fetched page")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if a.html:
        html = _read(a.html)
    else:
        logging.info("fetching %s", URL)
        html = fetch_page(URL, timeout=a.timeout, retries=a.retries)
        if not a.no_archive:
            logging.info("archived the page to %s", _archive(html, a.archive_dir))
    quarters = parse_page(html)
    counts = write_outputs(quarters, a.out_dir)
    _summary(quarters, counts, a.out_dir)
    return 0
