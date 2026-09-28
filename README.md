# SEBI PMR scraper — tables B, C, G and H

Extracts four tables from every SEBI **Portfolio Manager Monthly Report**, for
every registered portfolio manager, month by month, into one organised Excel
workbook (plus CSVs and a SQLite database).

Source: <https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doPmr=yes>

| SEBI table | Section | What is extracted |
|---|---|---|
| **B** — Break-up of assets under management | Discretionary | every AUM column, per investment approach |
| **C** — Funds Inflow/Outflow | Discretionary | **only** `Net Inflow (+ve)/ Outflow (-ve) during the month (in INR crores)` |
| **G** — Break-up of assets under management | Non-discretionary | every AUM column |
| **H** — Funds Inflow/Outflow | Non-discretionary | **only** the `Funds Inflow/Outflow During the Month` group (inflow, outflow, net) |

The twelve AUM columns are `Equity Listed/Unlisted`, `Plain Debt
Listed/Unlisted`, `Structured Debt Listed/Unlisted`, `Derivatives
Equity/Commodity/Others`, `Mutual Funds`, `Others`, `Total` — all in INR crores.

---

## Read this before you run it

### 1. Tables B, C, G and H do not exist before February 2021

This is the single most important finding, and it changes what "since 2018"
can mean.

SEBI replaced the monthly report format partway through. Fetching the same
manager across the archive shows three distinct eras:

| Period | What the portal returns |
|---|---|
| 2018-01 … 2020-11 | **Legacy layout** — "Types of Clients / No of Investors / Net AUM", *no lettered tables at all* and no per-investment-approach breakdown |
| 2020-12, 2021-01 | General Information only — the detail sections are absent |
| **2021-02 onwards** | **Current layout** — the lettered sections A–M, including B, C, G and H |

So the requested tables are simply not published for 2018, 2019, 2020 or
January 2021. There is no parsing trick that recovers them; the underlying
disclosure did not exist in that form. Scraping therefore **defaults to
`--since 2021-02`**, and everything earlier is recorded in the workbook's
`Coverage` sheet as `Legacy Format` rather than being silently dropped.

If you also want the legacy-era numbers, they are a *different* dataset with
different columns — run with `--since 2018-01` to archive those pages, and open
an issue describing which legacy fields you want mapped.

### 2. This is a big job — about 37 hours

~651 portfolio managers × ~68 published months ≈ **44,000 requests**. At the
polite 2.5–4.0 s spacing that is roughly **37 hours** of wall clock.

That shape drives the whole design:

* **Everything is checkpointed.** Every fetched cell is recorded, so stopping
  and restarting never re-requests what you already have.
* **Raw HTML is archived.** Parser bugs surface late; `reparse` rebuilds every
  derived row from disk with no network at all. Re-fetching would cost days.
* **Runs take a budget.** `--max-seconds` stops cleanly at the limit, and
  SIGTERM finishes the current request and exits — so a cancelled CI job
  loses nothing.

Practical split: do the **historical backfill locally** (or on a small VM)
where a multi-day run is fine, and let **CI handle the monthly increment** —
one month across all managers is ~651 requests, about 35 minutes.

### 3. Rate limiting

`robots.txt` allows this path and publishes **no `Crawl-delay`**:

```
User-agent: *
Disallow:
Disallow: /js
Disallow: /hindi/js
Disallow: /css
Disallow: /hindi/css
```

The 2.5–4.0 s window is therefore a self-imposed politeness budget, not a
site requirement. Each request waits a uniform random interval in that window.
On HTTP 429 or 503 the window **widens permanently for the rest of the run**
(it never narrows again on its own) and the request is retried with
exponential backoff, honouring `Retry-After`.

Sharding multiplies the aggregate rate: 4 shards at 2.5–4.0 s is effectively
one request every ~0.8 s against sebi.gov.in. Keep shard counts modest.

---

## Quick start

```bash
pip install -r requirements.txt

python -m sebi_pmr catalog                    # fetch the manager list (651 today)
python -m sebi_pmr scrape --limit 50          # try 50 cells first
python -m sebi_pmr status                     # coverage so far
python -m sebi_pmr excel -o output/sebi_pmr.xlsx
```

Then let the full backfill run — interrupt and restart it whenever you like:

```bash
python -m sebi_pmr scrape                     # 2021-02 .. last completed month
```

## Commands

| Command | Purpose |
|---|---|
| `catalog` | Refresh the portfolio-manager dropdown into the database |
| `scrape` | Fetch outstanding manager-months (`--since --until --shard --shards --limit --max-seconds --only --redo-errors`) |
| `reparse` | Rebuild every derived row from the HTML archive, no network |
| `merge` | Union shard databases into one (`merge 'shards/*.db' -o data/pmr.db`) |
| `excel` | Write the workbook and CSVs |
| `status` | Coverage summary |
| `probe-export` | Test whether the portal's own Excel/XML export can replace scraping |

Useful flags: `--min-delay/--max-delay` (default 2.5/4.0), `--only <name>` to
target one manager, `--no-archive` to skip keeping raw HTML.

## Output

`output/sebi_pmr.xlsx` — one sheet per table, each row a
(manager, month, investment approach) fact, with `Portfolio Manager`,
`Registration No`, `Year`, `Month`, `Month Name` and a real date `Period`
column so the sheets pivot directly.

| Sheet | Contents |
|---|---|
| `README` | Provenance, glossary, the February-2021 caveat |
| `B_Disc_AUM` | Table B, all AUM columns, discretionary |
| `C_Disc_NetFlow` | Table C, net inflow/outflow during the month |
| `G_NonDisc_AUM` | Table G, all AUM columns, non-discretionary |
| `H_NonDisc_Flow` | Table H, inflow / outflow / net during the month |
| `Data_Quality` | Filed rows whose components do not sum to SEBI's stated Total |
| `Coverage` | Per-month fetch outcomes, so gaps are visible rather than silent |

The same tables are written as CSVs to `output/csv/` — easier than Excel at
several hundred thousand rows. A full run produces roughly 354,000 rows in
`B_Disc_AUM` alone; building that workbook takes about two minutes and peaks
near 1.1 GB of RAM, so prefer the CSVs in memory-constrained environments. A table exceeding Excel's 1,048,576-row cap is
split into `_pt2`, `_pt3` … sheets rather than truncated.

### Data quality

Extraction is validated against SEBI's own arithmetic: for every fixture, the
published **Total row equals the sum of the approach rows we parsed, in all
twelve columns**. That is the strongest available check that columns are mapped
correctly — a single shifted column breaks it immediately.

Some *individual filed rows* still do not self-reconcile: in February and March
2021, the first months of the new format, several rows have components summing
past their own stated Total. Those are inconsistencies in the filings, not
extraction errors, and they are listed in the `Data_Quality` sheet rather than
quietly corrected. Numbers are reproduced exactly as SEBI publishes them.

Two more things worth knowing when joining this data:

* **Managers rebrand.** IIFL Asset Management and 360 ONE Asset Management are
  the same registration number (`INP000004565`). Join on `Registration No`;
  the workbook shows the most recently published name.
* **`Is Total Row`** marks SEBI's own Total line — filter it out before summing.

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `tests.yml` | push / PR | Runs the suite against archived fixtures — never touches the portal |
| `monthly-update.yml` | 15th & 25th monthly, or manual | Re-scrapes a trailing 3-month window to catch late filers, rebuilds the workbook |
| `backfill.yml` | manual | Sharded historical sweep with cached per-shard state and a wall-clock budget |

`backfill.yml` gives each shard its own SQLite file so parallel shards never
contend, caches that file between runs so re-running resumes, and merges the
shards into one workbook at the end. Re-run it until `status` reports full
coverage.

## A possible fast path (unverified)

The portal has **Download Excel** and **Download XML** buttons. Their handlers
in `/sebiweb/js/reportDownload.js` post to
`OtherAction.do?doPmrExcel=yes` with `format=excel|xml` — and, notably, their
validators check only **year and month, not the manager id**:

```js
function getPMRExcel() {
    if (checkPMR()) {                       // checks year + month only
        document.forms[0].format.value = "excel";
        document.otherForm.action = "/sebiweb/other/OtherAction.do?doPmrExcel=yes";
        document.otherForm.submit();
    }
}
```

If that export returns a whole month without a manager id, the job collapses
from ~44,000 requests to ~68. This could not be confirmed from the environment
this was built in (the export returns a file attachment, which the available
fetch tooling could not materialise), so the scraper uses the proven HTML path
by default. Check it yourself in one command:

```bash
python -m sebi_pmr probe-export --period 2024-03 --save /tmp/probe
```

It walks the whole matrix — {GET, POST} x {with manager id, without} x
{xml, excel} — sniffs what each response actually is (real XLSX, XML, an empty
body, or the portal simply re-rendering its form), saves the payloads, and
prints a verdict. If the whole-month variant returns a real file, that is worth
building on before committing to a 37-hour scrape.

## Layout

```
sebi_pmr/
  tables.py    span-aware HTML table -> dense grid, number parsing
  parse.py     locate and extract tables B, C, G, H
  fetch.py     rate-limited session, retries, manager catalogue
  store.py     SQLite checkpointing, HTML archive, shard merge
  pipeline.py  work planning, sharding, budgets, graceful shutdown
  excel.py     workbook and CSV output
tests/         65 tests, all offline
  fixtures/    real captured pages, one per format era
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The fixtures are real pages captured from the portal covering every format
era — legacy (2018/2020), the general-information-only gap (Dec 2020 / Jan
2021), the first months of the new format (Feb–Apr 2021), the current format
(2024), and a small manager whose sections differ from a large one. The suite
needs no network.

---

# SEBI AIF statistics — Category III AIFs

`sebi_aif` extracts the **Category III AIF row, every column**, from two tables
on SEBI's [Data relating to activities of Alternative Investment Funds](https://www.sebi.gov.in/statistics/1392982252002.html)
page, for every quarter the page carries:

| SEBI table | Columns (Rs crore, cumulative net) | Quarters |
|---|---|---|
| **Cumulative net figures as at the end of the quarter ending …** | Commitments Raised, Funds Raised, Investments Made | Sep 2012 → latest (56 as of Jun 2026) |
| **Cumulative net investment made in equity and debt securities** | Equity/equity linked securities, Debt securities, Units of AIFs/REITs/InVITs, Security Receipts | Mar 2024 → latest (10) |

```bash
python -m sebi_aif                          # one request, ~3 s; writes output/aif/
python -m sebi_aif --html saved.html.gz     # re-parse a saved copy, no network
```

The whole history lives on one static page, so a run is a single GET and
rebuilds everything; new quarters are picked up as SEBI adds them. Each fetched
page is archived under `data/aif/raw/`, because SEBI edits the page in place.

### Output — `output/aif/` (committed: a few KB, and diffs show SEBI's revisions)

| File | Contents |
|---|---|
| `sebi_aif_category3.xlsx` | `README`, `CatIII_NetFigures`, `CatIII_EquityDebt`, `CatIII_Combined` |
| `csv/CatIII_NetFigures.csv` | the headline table, one row per quarter |
| `csv/CatIII_EquityDebt.csv` | the equity and debt table, one row per quarter |
| `csv/CatIII_Combined.csv` | both side by side (equity and debt blank before Mar 2024) |

Rows run oldest first with `Quarter End` (a real date), `Quarter`, and India's
April–March `Financial Year` / `FY Quarter`, so the sheets chart and pivot directly.

### Quirks of the source page

* **Four September quarters are headed "December 31".** The sections for
  Sep 2020, 2021, 2022 and 2023 repeat the heading of the quarter above them.
  Sections run strictly newest first, one quarter apart, and the industry
  Grand Total rises in every one, so a repeated heading is re-dated by position
  and flagged in the `Notes` column. Business Standard's September 2023 figures
  (Rs 9.54 trn committed; Rs 78,686 crore invested by Category III) match the
  re-dated section exactly. Any other break in the sequence stops the run
  rather than being guessed at.
* **Commented-out HTML.** Retired content, including a whole table, sits inside
  `<!-- -->`; comments are stripped before anything is located.
* **Formatting drift.** Headings ("March 31 st , 2024", "30th June 2018") and
  figures ("1,28,058.26", "46824.91") vary by year, so dates are parsed
  leniently and columns are matched on header text; a column SEBI adds later is
  kept instead of being dropped or misaligned.
* **`-` means nil** and is left blank. Figures are reproduced exactly as
  published; Category III cumulative figures do fall in some quarters (e.g.
  Sep 2019, Jun 2020, Sep 2022).

Code: `sebi_aif/parse.py` (sections, dating, Category III rows), `fetch.py`,
`excel.py`, `cli.py`, reusing `sebi_pmr/tables.py` for span-aware table
expansion. Tests: `tests/test_aif.py`, offline against a captured copy of the page.
