"""Command line entry point: ``python -m sebi_pmr <command>``."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .excel import write_workbook
from .fetch import PmrClient
from .parse import NEW_FORMAT_START
from .pipeline import (default_end, months_between, parse_period, reparse, run,
                       select_shard)
from .store import Store, merge as merge_stores


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _client(a) -> PmrClient:
    return PmrClient(min_delay=a.min_delay, max_delay=a.max_delay,
                     timeout=a.timeout, retries=a.retries)


def cmd_catalog(a) -> int:
    store, client = Store(a.db, a.archive_dir), _client(a)
    html = client.warm()
    pms = client.portfolio_managers(html)
    store.upsert_pms(pms)
    print(f"catalogued {len(pms)} portfolio managers")
    print(f"years offered by the portal: {client.available_years(html)}")
    store.close()
    return 0


def cmd_scrape(a) -> int:
    store, client = Store(a.db, a.archive_dir), _client(a)
    pms = [p for p in store.pms()]
    if not pms:
        logging.info("catalogue empty - fetching it first")
        pms_obj = client.portfolio_managers(client.warm())
        store.upsert_pms(pms_obj)
        pms = store.pms()

    from .fetch import PortfolioManager
    objs = [PortfolioManager(r["pmr_id"], r["reg_no"], r["name"]) for r in pms]
    if a.only:
        needle = a.only.lower()
        objs = [p for p in objs if needle in p.name.lower() or needle in p.reg_no.lower()]
        if not objs:
            print(f"no manager matches {a.only!r}", file=sys.stderr)
            return 2
    objs = select_shard(objs, a.shard, a.shards)

    start = parse_period(a.since) if a.since else NEW_FORMAT_START
    end = parse_period(a.until) if a.until else default_end()
    periods = months_between(start, end)
    logging.info("shard %d/%d: %d managers x %d months",
                 a.shard, a.shards, len(objs), len(periods))

    stats = run(store, client, objs, periods, limit=a.limit,
                max_seconds=a.max_seconds, archive=not a.no_archive,
                redo_errors=a.redo_errors, method=a.method)
    print(stats.summary())
    store.close()
    return 0


def cmd_reparse(a) -> int:
    store = Store(a.db, a.archive_dir)
    stats = reparse(store)
    print(stats.summary())
    store.close()
    return 0


def cmd_excel(a) -> int:
    store = Store(a.db, a.archive_dir)
    counts = write_workbook(store.conn, a.out, csv_dir=a.csv_dir)
    for name, n in counts.items():
        print(f"  {name:<18} {n:>9,} rows")
    print(f"wrote {a.out}")
    store.close()
    return 0


def cmd_merge(a) -> int:
    import glob
    inputs = []
    for pat in a.inputs:
        inputs.extend(sorted(glob.glob(pat)))
    inputs = [p for p in inputs if os.path.abspath(p) != os.path.abspath(a.out)]
    if not inputs:
        print("no input databases matched", file=sys.stderr)
        return 2
    counts = merge_stores(inputs, a.out, a.archive_dir)
    print(f"merged {len(inputs)} shard databases into {a.out}")
    for t, n in counts.items():
        print(f"  {t:<12} {n:>9,} rows")
    return 0


def cmd_status(a) -> int:
    store = Store(a.db, a.archive_dir)
    cov = store.coverage()
    print(json.dumps(cov, indent=2))
    total = cov["managers"] * len(months_between(NEW_FORMAT_START, default_end()))
    attempted = sum(cov["pages_by_status"].values())
    if total:
        print(f"\nnew-format grid: {attempted:,} / {total:,} cells "
              f"({attempted / total * 100:.1f}%)")
    store.close()
    return 0


def cmd_probe_export(a) -> int:
    """Check whether the portal's Excel/XML export can replace page scraping.

    The portal's own ``getPMRExcel``/``getPMRXml`` validate only year and
    month, so an export without a manager id *might* return a whole month at
    once - which would turn ~44 000 requests into ~68.  This probes it.
    """
    store, client = Store(a.db, a.archive_dir), _client(a)
    pms = store.pms()
    from .fetch import PortfolioManager
    one = PortfolioManager(pms[0]["pmr_id"], pms[0]["reg_no"], pms[0]["name"]) if pms else None
    y, m = parse_period(a.period)
    for label, pm in (("whole-month (no pmrId)", None), ("single manager", one)):
        if label == "single manager" and one is None:
            continue
        for fmt in ("xml", "excel"):
            try:
                body, ctype = client.export_bytes(pm, y, m, fmt=fmt, method=a.method)
            except Exception as exc:
                print(f"{label:<24} {fmt:<6} FAILED {type(exc).__name__}: {exc}")
                continue
            head = body[:120].replace(b"\n", b" ")
            print(f"{label:<24} {fmt:<6} {len(body):>10,} bytes  {ctype}\n"
                  f"{'':24} head={head!r}")
            if a.save and body:
                path = f"{a.save}/export_{label.split()[0]}_{fmt}_{y}{m:02d}.bin".replace(" ", "")
                import os
                os.makedirs(a.save, exist_ok=True)
                open(path, "wb").write(body)
                print(f"{'':24} saved -> {path}")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sebi-pmr",
        description="Scrape SEBI portfolio-manager monthly reports (tables B, C, G, H).")
    p.add_argument("--db", default="data/pmr.db")
    p.add_argument("--archive-dir", default="data/raw")
    p.add_argument("-v", "--verbose", action="store_true")

    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--min-delay", type=float, default=2.5,
                     help="floor of the per-request delay window (default 2.5s)")
    net.add_argument("--max-delay", type=float, default=4.0,
                     help="ceiling of the per-request delay window (default 4.0s)")
    net.add_argument("--timeout", type=float, default=60.0)
    net.add_argument("--retries", type=int, default=4)
    net.add_argument("--method", choices=("post", "get"), default="post")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("catalog", parents=[net], help="refresh the manager list")
    sp.set_defaults(func=cmd_catalog)

    sp = sub.add_parser("scrape", parents=[net], help="fetch outstanding manager-months")
    sp.add_argument("--since", help="first period YYYY-MM (default 2021-02, the format change)")
    sp.add_argument("--until", help="last period YYYY-MM (default: last completed month)")
    sp.add_argument("--shard", type=int, default=0)
    sp.add_argument("--shards", type=int, default=1)
    sp.add_argument("--limit", type=int, help="stop after N pages")
    sp.add_argument("--max-seconds", type=float, help="wall-clock budget for this run")
    sp.add_argument("--only", help="restrict to managers matching this substring")
    sp.add_argument("--no-archive", action="store_true", help="do not keep raw HTML")
    sp.add_argument("--redo-errors", action="store_true", help="retry cells that previously errored")
    sp.set_defaults(func=cmd_scrape)

    sp = sub.add_parser("reparse", help="rebuild rows from the archive (no network)")
    sp.set_defaults(func=cmd_reparse)

    sp = sub.add_parser("excel", help="write the workbook")
    sp.add_argument("-o", "--out", default="output/sebi_pmr.xlsx")
    sp.add_argument("--csv-dir", default="output/csv")
    sp.set_defaults(func=cmd_excel)

    sp = sub.add_parser("merge", help="combine shard databases into one")
    sp.add_argument("inputs", nargs="+", help="shard db paths or globs")
    sp.add_argument("-o", "--out", default="data/pmr.db")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("status", help="coverage summary")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("probe-export", parents=[net],
                        help="test the portal's Excel/XML export fast path")
    sp.add_argument("--period", default="2024-03")
    sp.add_argument("--save", help="directory to save returned payloads into")
    sp.set_defaults(func=cmd_probe_export)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
