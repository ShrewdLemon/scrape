# SEBI PMR scraper: tables B, C, G and H

[![tests](https://github.com/ShrewdLemon/scrape/actions/workflows/tests.yml/badge.svg)](https://github.com/ShrewdLemon/scrape/actions/workflows/tests.yml)

A scraper for SEBI's **Portfolio Manager Monthly Reports**. It pulls four tables
for every registered portfolio manager, month by month, into one Excel workbook,
plus CSVs and a SQLite database. It is for analysts and data teams who track
PMS assets and flows and are tired of clicking through the portal one manager
at a time.

Source: <https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doPmr=yes>

| SEBI table | Section | What is extracted |
|---|---|---|
| **B**: Break-up of assets under management | Discretionary | every AUM column, per investment approach |
| **C**: Funds Inflow/Outflow | Discretionary | **only** `Net Inflow (+ve)/ Outflow (-ve) during the month (in INR crores)` |
| **G**: Break-up of assets under management | Non-discretionary | every AUM column |
| **H**: Funds Inflow/Outflow | Non-discretionary | **only** the `Funds Inflow/Outflow During the Month` group (inflow, outflow, net) |

The twelve AUM columns are `Equity Listed/Unlisted`, `Plain Debt
Listed/Unlisted`, `Structured Debt Listed/Unlisted`, `Derivatives
Equity/Commodity/Others`, `Mutual Funds`, `Others` and `Total`, all in INR crores.

## Install

Python 3.11 is what CI uses.

```bash
git clone https://github.com/ShrewdLemon/scrape
cd scrape
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Quickstart

These commands were run against the live portal on 29 Sep 2026. The first three
make a handful of requests.

```bash
python -m sebi_pmr catalog                    # fetch the manager list (651 on 29 Sep 2026)
python -m sebi_pmr roster --since 2026-07 --until 2026-07   # who filed that month: 1 request
python -m sebi_pmr scrape --since 2026-07 --until 2026-07 --only "360 ONE" --limit 2
python -m sebi_pmr status                     # coverage so far
python -m sebi_pmr excel -o output/sebi_pmr.xlsx
```

Then let the full backfill run. Interrupt and restart it whenever you like:

```bash
python -m sebi_pmr roster                     # one request per month, lets scrape skip non-filers
python -m sebi_pmr scrape                     # 2021-02 .. last completed month
```

State lives in `data/pmr.db` and raw pages in `data/raw/` by default
(`--db` and `--archive-dir` change both).

## Features

* **Checkpointed fetching.** Every fetched manager-month is recorded, so a
  stopped run restarts without re-requesting what it already has.
* **Raw HTML archive.** Pages are kept on disk. `reparse` rebuilds every derived
  row from the archive with no network, so a parser fix never costs a re-fetch.
* **Filing roster.** The portal's consolidated export lists every manager that
  filed in a month. `roster` records that list (one request per month) and
  `scrape` skips managers that did not file.
* **Run budgets.** `--max-seconds` stops cleanly at a limit, and SIGTERM
  finishes the current request and exits, so a cancelled CI job loses nothing.
* **Sharding.** `--shard/--shards` split the work, each shard in its own SQLite
  file, and `merge` unions them.
* **Polite rate limiting.** A random 2.5 to 4.0 s gap between requests. On
  HTTP 429 or 503 the gap widens for the rest of the run and the request is
  retried with backoff, honouring `Retry-After`.
* **GitHub Actions monthly update.** A scheduled workflow re-scrapes a trailing
  3-month window twice a month and uploads the workbook as a build artifact.

## Commands

| Command | Purpose |
|---|---|
| `catalog` | Refresh the portfolio-manager dropdown into the database |
| `roster` | Record who filed each month from the consolidated export (1 request per month) |
| `scrape` | Fetch outstanding manager-months (`--since --until --shard --shards --limit --max-seconds --only --redo-errors --no-roster`) |
| `reparse` | Rebuild every derived row from the HTML archive, no network |
| `merge` | Union shard databases into one (`merge 'shards/*.db' -o data/pmr.db`) |
| `excel` | Write the workbook and CSVs |
| `status` | Coverage summary |
| `inspect-export` | Report what the month-wide export contains |
| `probe-export` | Test the portal's Excel/XML export variants |

Useful flags: `--min-delay/--max-delay` (default 2.5/4.0), `--only <name>` to
target one manager, `--no-archive` to skip keeping raw HTML.

## Read this before you run it

### 1. Tables B, C, G and H do not exist before February 2021

SEBI changed the monthly report format partway through. Fetching the same
manager across the archive shows three eras:

| Period | What the portal returns |
|---|---|
| 2018-01 … 2020-11 | **Legacy layout**: "Types of Clients / No of Investors / Net AUM", no lettered tables and no per-investment-approach breakdown |
| 2020-12, 2021-01 | General Information only. The detail sections are absent |
| **2021-02 onwards** | **Current layout**: lettered sections A to M, including B, C, G and H |

So the four tables are not published for 2018, 2019, 2020 or January 2021. No
parsing trick recovers them. Scraping **defaults to `--since 2021-02`**, and
earlier months are recorded in the workbook's `Coverage` sheet as
`Legacy Format` rather than silently dropped.

If you want the legacy-era numbers, they are a different dataset with different
columns. Run with `--since 2018-01` to archive those pages.

### 2. A full backfill is a long job

The catalogue lists 651 managers. On 29 Sep 2026 the new-format grid
(February 2021 to the last completed month) was 43,483 manager-months. At 2.5 to
4.0 s per request that is well over a day of wall clock, before the roster
removes non-filers. For March 2024 the consolidated export listed 415 filers
out of 651 registered managers, so the roster cuts a large share of requests.

Practical split: run the **historical backfill locally** (or on a small VM)
and let **CI handle the monthly increment**.

### 3. Rate limiting

`robots.txt` allows this path and publishes no `Crawl-delay`. The 2.5 to 4.0 s
window is a self-imposed politeness budget, not a site requirement. Sharding
multiplies the aggregate rate: 4 shards at 2.5 to 4.0 s is roughly one request
every 0.8 s against sebi.gov.in. Keep shard counts modest.

### 4. The consolidated export is a roster, not a shortcut

The portal's **Download Excel** button posts to `OtherAction.do?doPmrExcel=yes`.
It ignores the manager id and returns one consolidated sheet for the whole
month. That sheet has one row per manager, so it cannot carry tables B and C,
which are per investment approach. The scraper uses it only to learn who filed.
`inspect-export` and `probe-export` are kept for checking this yourself.

## Output

`output/sebi_pmr.xlsx` has one sheet per table. Each row is a
(manager, month, investment approach) fact, with `Portfolio Manager`,
`Registration No`, `Year`, `Month`, `Month Name` and a real date `Period`
column, so the sheets pivot directly.

| Sheet | Contents |
|---|---|
| `README` | Provenance, glossary, the February-2021 caveat |
| `B_Disc_AUM` | Table B, all AUM columns, discretionary |
| `C_Disc_NetFlow` | Table C, net inflow/outflow during the month |
| `G_NonDisc_AUM` | Table G, all AUM columns, non-discretionary |
| `H_NonDisc_Flow` | Table H, inflow / outflow / net during the month |
| `Data_Quality` | Filed rows whose components do not sum to SEBI's stated Total |
| `Coverage` | Per-month fetch outcomes, so gaps are visible rather than silent |

The same tables are written as CSVs to `output/csv/`, which are easier than
Excel at several hundred thousand rows. A table over Excel's 1,048,576-row cap
is split into `_pt2`, `_pt3` … sheets rather than truncated.

Two things to know when joining this data:

* **Managers rebrand.** IIFL Asset Management and 360 ONE Asset Management share
  one registration number (`INP000004565`). Join on `Registration No`. The
  workbook shows the most recently published name.
* **`Is Total Row`** marks SEBI's own Total line. Filter it out before summing.

## How it's tested

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

112 tests, all offline (112 passed on 29 Sep 2026). CI runs them on every push
and pull request. The fixtures in `tests/fixtures/` are real pages captured from
the portal, covering every format era: legacy (2018 and 2020), the
general-information-only gap (Dec 2020 and Jan 2021), the first months of the
new format (Feb to Apr 2021), the current format (2023 and 2024), and small
managers whose sections differ from a large one.

The main check is SEBI's own arithmetic. For every fixture, the published
**Total row equals the sum of the parsed approach rows in all twelve columns**.
A single shifted column breaks that at once.

## Design principles

* **No fabricated or corrected numbers.** Figures are reproduced exactly as SEBI
  publishes them. Some filed rows do not reconcile (in February and March 2021,
  several rows sum past their own Total). Those go to `Data_Quality`, not into
  a quiet fix.
* **Gaps are visible.** Every manager-month has a recorded outcome
  (ok, no data, legacy, error), shown in `Coverage`.
* **Never re-fetch what you have.** Checkpoints plus the raw archive mean parser
  work is offline.
* **Be polite to the source.** Slow by default, backs off on throttling.

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `tests.yml` | push / PR | Runs the suite against archived fixtures. Never touches the portal |
| `monthly-update.yml` | 15th and 25th monthly, or manual | Re-scrapes a trailing 3-month window to catch late filers, rebuilds the workbook |
| `backfill.yml` | manual | Sharded historical sweep with cached per-shard state and a wall-clock budget |
| `smoke-test.yml` | manual, or a push touching `.github/run-smoke-test` | A few live requests to check the portal path end to end |

The workbook from `monthly-update` and `backfill` is uploaded as a workflow
artifact. It is not committed to the repo.

## Layout

```
sebi_pmr/
  tables.py    span-aware HTML table -> dense grid, number parsing
  parse.py     locate and extract tables B, C, G, H
  fetch.py     rate-limited session, retries, manager catalogue
  roster.py    per-month filing roster from the consolidated export
  store.py     SQLite checkpointing, HTML archive, shard merge
  pipeline.py  work planning, sharding, budgets, graceful shutdown
  excel.py     workbook and CSV output
  inspect.py   describe what the month-wide export contains
tests/         offline test suite
  fixtures/    real captured pages, one per format era
```

## Limitations and roadmap

* Tables B, C, G and H only. The other lettered sections are not parsed.
* Legacy-era pages (before February 2021) are archived but not mapped to columns.
* The parser follows the portal's current HTML. If SEBI changes the layout, the
  fixtures will not catch it until a live run fails. Run `smoke-test` first.
* `monthly-update` carries `data/pmr.db` between runs in the Actions cache.
  GitHub removes a cache that is not used for 7 days, and the runs are 10 and
  about 20 days apart, so the state often does not survive. Each run still
  re-scrapes its full 3-month window, so the output is complete for that window.
  Durable storage for the database is on the roadmap.

## Data source and reuse

All data comes from SEBI's public Portfolio Manager Monthly Reports. Credit
**Securities and Exchange Board of India (SEBI)** as the source when you use
it. Check SEBI's website terms before any commercial reuse, which may need
SEBI's permission. This project is not affiliated with or endorsed by SEBI.

## Licence

No licence file has been chosen yet, so default copyright applies to the code.
Open an issue if you want to reuse it.

## Contact

[github.com/ShrewdLemon](https://github.com/ShrewdLemon)
