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


def _sniff(body: bytes) -> str:
    """Name what the export actually returned, from its first bytes."""
    if not body:
        return "EMPTY (zero bytes)"
    head = body.lstrip(b"\xef\xbb\xbf")          # tolerate a UTF-8 BOM
    if not head:
        return "EMPTY (byte-order mark only)"
    if head[:2] == b"PK":
        return "XLSX/ZIP archive"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "legacy XLS (OLE2)"
    low = head[:400].lower()
    if low.startswith(b"<?xml") or low.startswith(b"<workbook") or low.startswith(b"<pmr"):
        return "XML document"
    if b"<html" in low or b"<!doctype html" in low:
        return "HTML page (the export did not fire - portal re-rendered the form)"
    if head[:4] == b"%PDF":
        return "PDF"
    return "unrecognised binary/text"


def cmd_probe_export(a) -> int:
    """Check whether the portal's Excel/XML export can replace page scraping.

    The portal's own ``getPMRExcel``/``getPMRXml`` validate only year and
    month, so an export without a manager id *might* return a whole month at
    once - which would turn ~44,000 requests into ~68.  This walks the full
    matrix of {GET, POST} x {with manager, without} x {xml, excel} and says
    what each one returned, because the interesting answer is which
    combination, if any, yields a real file.
    """
    store, client = Store(a.db, a.archive_dir), _client(a)
    pms = store.pms()
    from .fetch import PortfolioManager
    one = (PortfolioManager(pms[0]["pmr_id"], pms[0]["reg_no"], pms[0]["name"])
           if pms else None)
    if one is None:
        print("catalogue is empty - run `catalog` first so a manager id is available",
              file=sys.stderr)
        return 2
    year, month = parse_period(a.period)
    methods = ("post", "get") if a.method == "both" else (a.method,)

    print(f"probing the export for {year}-{month:02d}  "
          f"(manager: {one.name})\n")
    header = f"{'method':<7}{'scope':<16}{'fmt':<7}{'status':<8}{'bytes':>12}  what came back"
    print(header)
    print("-" * len(header))

    whole_month_win = None
    for method in methods:
        for scope, pm in (("whole-month", None), ("single-manager", one)):
            for fmt in ("xml", "excel"):
                try:
                    body, ctype, status = client.export_bytes(pm, year, month,
                                                              fmt=fmt, method=method)
                except Exception as exc:
                    print(f"{method:<7}{scope:<16}{fmt:<7}{'-':<8}{'-':>12}  "
                          f"FAILED {type(exc).__name__}: {exc}")
                    continue
                kind = _sniff(body)
                print(f"{method:<7}{scope:<16}{fmt:<7}{status:<8}{len(body):>12,}  {kind}")
                if ctype:
                    print(f"{'':30}content-type: {ctype}")
                if a.save and body:
                    os.makedirs(a.save, exist_ok=True)
                    path = os.path.join(
                        a.save, f"export_{method}_{scope}_{fmt}_{year}{month:02d}.bin")
                    with open(path, "wb") as fh:
                        fh.write(body)
                    print(f"{'':30}saved -> {path}")
                usable = len(body) > 2048 and "EMPTY" not in kind and "did not fire" not in kind
                if usable and scope == "whole-month" and whole_month_win is None:
                    whole_month_win = (method, fmt, len(body))

    print()
    if whole_month_win:
        method, fmt, size = whole_month_win
        print(f"VERDICT: the whole-month export WORKS ({method.upper()}, format={fmt}, "
              f"{size:,} bytes).")
        print("         If that payload really covers every manager, the job drops from")
        print("         ~44,000 requests to ~68. Inspect the saved file before relying on it.")
    else:
        print("VERDICT: no whole-month export. Keep the per-page HTML path (the default).")
    store.close()
    return 0


def cmd_inspect_export(a) -> int:
    """Report what the month-wide export actually contains.

    The export ignores ``pmrId``, so it is very likely one document per month
    covering every manager. This says whether the fields behind tables B, C,
    G and H are in there, which decides whether the ~44,000-request HTML sweep
    can be replaced by ~68 export downloads.
    """
    from .inspect import summarise

    if a.file:
        with open(a.file, "rb") as fh:
            body = fh.read()
        print(f"inspecting {a.file}\n")
        print(summarise(body, a.format))
        return 0

    client = _client(a)
    year, month = parse_period(a.period)
    body, ctype, status = client.export_bytes(None, year, month,
                                              fmt=a.format, method="post")
    print(f"fetched {year}-{month:02d} export: HTTP {status}, {len(body):,} bytes, {ctype}\n")
    if a.save:
        os.makedirs(os.path.dirname(os.path.abspath(a.save)), exist_ok=True)
        with open(a.save, "wb") as fh:
            fh.write(body)
        print(f"saved -> {a.save}\n")
    print(summarise(body, a.format))
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
    sp.add_argument("--method", choices=("post", "get"), default="post",
                    help="how to submit the report form (the site itself POSTs)")
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

    sp = sub.add_parser("inspect-export", parents=[net],
                        help="report what the month-wide export contains")
    sp.add_argument("--period", default="2024-03")
    sp.add_argument("--format", choices=("xml", "excel"), default="xml")
    sp.add_argument("--file", help="inspect a saved payload instead of fetching")
    sp.add_argument("--save", help="write the fetched payload to this path")
    sp.set_defaults(func=cmd_inspect_export)

    sp = sub.add_parser("status", help="coverage summary")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("probe-export", parents=[net],
                        help="test the portal's Excel/XML export fast path")
    sp.add_argument("--period", default="2024-03")
    sp.add_argument("--save", help="directory to save returned payloads into")
    sp.add_argument("--method", choices=("post", "get", "both"), default="both",
                    help="which HTTP methods to try (default: both)")
    sp.set_defaults(func=cmd_probe_export)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
