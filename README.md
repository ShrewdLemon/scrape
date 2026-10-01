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
| `nsdl-fpi-equity.yml` | 3rd monthly, or manual | NSDL FPI monthly equity flows, 2002 to date (~2 min) — see below |
| `nsdl-fpi-daily-equity.yml` | manual | NSDL FPI **daily** equity, Jan 2001 – Jan 2020 (~8 min) — see below |

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

# NSDL FPI equity flows — monthly, 2002 to date

A separate, much smaller scraper (`nsdl_fpi/`) for NSDL's **Monthly FPI Net
Investments** report: <https://www.fpi.nsdl.co.in/web/Reports/Yearwise.aspx?RptType=6>.
It takes **only the Equity column**, for every month of every year since 2002,
in both INR Crores and USD Million.

```bash
python -m nsdl_fpi                        # 2002 .. latest, INR + USD
python -m nsdl_fpi --currency INR         # INR only (half the requests)
python -m nsdl_fpi --offline              # rebuild from output/raw, no network
```

A snapshot of the result is committed at
`outputs/NSDL_FPI_Equity_Monthly_2002-2026.xlsx`.

### How it works, and how long it takes

The page is ASP.NET WebForms: the year dropdown and the INR/USD switch are both
`__doPostBack` round-trips carrying `__VIEWSTATE`. So **one year in one
currency is one POST** — 25 years × 2 currencies = **50 requests**. Each
response takes ~0.4 s; with the default 1–2 s politeness gap the full history
scrapes in **about 75 seconds** (≈2 minutes for the whole CI job, including
setup). The server drops connections from non-browser user agents, so the
client sends a browser UA.

| Job | Requests | Time |
|---|---|---|
| Full history, INR + USD | 50 | ~75 s |
| Full history, one currency | 25 | ~40 s |
| `--offline` rebuild from archived pages | 0 | < 1 s |
| `nsdl-fpi-equity.yml` end to end | 50 | ~2 min |

### Getting the right "Equity"

The table has changed shape four times — 2002–16: Equity/Debt/Total;
2017–19 adds Hybrid; 2020–23 adds Debt-VRR; **2024 onward adds Mutual Funds
(with its own `Equity` sub-column) and AIFs**. The FPI Equity column is always the *first* leaf column; the parser
anchors on that and refuses the page (`FormatError`) if the first leaf header
is ever not `Equity`, or if the year or unit on the page isn't what was asked
for.

Every year is reconciled: the sum of the parsed months must equal NSDL's own
`Total - YYYY` row. All 50 pages reconcile exactly. `--strict` (used in CI)
fails the run if any don't.

### Workbook

| Sheet | Contents |
|---|---|
| `README` | Source, units, scrape time, caveats |
| `Equity_Monthly` | One row per month: `Period` (real date), `Year`, `Month`, `Month Name`, `Equity (INR Crores)`, `Equity (USD Million)` |
| `INR_by_Year` | Years down, Jan–Dec across, NSDL's annual total, plus a bar chart of annual flows |
| `USD_by_Year` | The same in USD Million |
| `Check` | Sum of months vs NSDL's stated total, per year and currency |

The current year is partial (only months NSDL has published), and NSDL notes
recent figures are compiled from custodian reports and can be revised — the
monthly workflow re-scrapes the whole history each run, so revisions flow in.

# NSDL FPI equity — daily, Jan 2001 to Jan 2020

`nsdl_fpi/daily/` scrapes NSDL's **Archive (Trends in FPI/FII Investments)**
report, <https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx>, and keeps only
the **Equity** row for every reporting day.

```bash
python -m nsdl_fpi.daily                          # 2001-01 .. 2020-01 -> output/nsdl_fpi_daily_equity.xlsx
python -m nsdl_fpi.daily --start 2008-01 --end 2009-12
python -m nsdl_fpi.daily --offline                # rebuild from output/raw_daily, no network
```

## How the page works, and why it is only 229 requests

The page takes one input, a "To Date", and returns **every reporting day of
that month up to the date**, plus month / year / grand totals (and a
derivatives table, which is ignored). Asking for each month-end therefore
returns the whole month. Jan 2001 to Jan 2020 is 229 postbacks, not ~4,800
daily ones. Pages are cached in `--raw-dir`, so a re-run only fetches what is
missing.

| Step | Time |
|---|---|
| Fetch 229 month pages (1–2 s polite gap, ~0.5 s per response) | **5 m 46 s** measured |
| Parse + reconcile + write Excel/CSV | ~9 s |
| Re-run with the raw pages cached (`--offline`) | ~9 s |
| GitHub Actions job end-to-end (setup + scrape + upload) | ~7–8 min estimated |

## Which row is "Equity"

| Period | Layout | Equity value taken |
|---|---|---|
| 2001-01 … 2009-11 | flat: one `Equity` and one `Debt` row per day | the `Equity` row |
| 2009-12 … 2020-01 | routed: each category split into Stock Exchange / Primary market & others / Sub-total, then a day `Total`; Hybrid from 2017-11, Debt-VRR in 2020-01 | the **Equity Sub-total** |

## Investment routes (Stock Exchange / Primary market / Sub-total)

From 2009-12 NSDL splits each day's equity into **Stock Exchange**, **Primary
market & others** and a **Sub-total**. All three rows are kept, each with gross
purchases, gross sales, net Rs Cr and net US$ mn. They go to the
`Equity_Routes` sheet and to `output/nsdl_fpi_daily_equity_routes.csv`, which
covers 2,446 days. Before 2009-12 the page has one undivided Equity row, so
there is no route split to scrape for those years.

Two checks back this up:

* **Daily:** Stock Exchange + Primary = Sub-total, to within 0.1 crore.
* **Monthly:** each route's daily sum matches NSDL's `Total for <Month>` row
  for that route, to within 0.4. This is the `Routes_Monthly` sheet, and
  `--strict` covers it too.

## Fallback

If a day has no equity figures, the parser falls back to that day's `Total`
row. The flat layout has no Total row, so there it uses the sum of the
categories. Such days are marked in the `Basis` column, highlighted, and listed
on the `Fallbacks` sheet. On the real 2001–2020 data the fallback never
triggers: all 4,660 days have an equity row.

## Reconciliation

Each month's daily equity figures are summed and compared with NSDL's own
`Total for <Month>` equity row. All 229 months agree to within 0.4 crore /
0.4 US$ mn, which is two-decimal rounding. `--strict` fails the run if any
month is off by more than 1.

## Workbook

| Sheet | Contents |
|---|---|
| `README` | Source, coverage, basis rules, units |
| `Equity_Daily` | Date, Basis, Gross Purchases, Gross Sales, Net (Rs Cr), Net (US$ mn), USD/INR rate — one row per reporting day |
| `Monthly` | Daily sums vs NSDL's month total, with the difference and an OK flag |
| `INR_by_Year` | Net equity per month (Rs Cr), years down, with an annual bar chart |
| `Equity_Routes` | Per day from 2009-12: Stock Exchange / Primary market & others / Sub-total × (Gross Purchases, Gross Sales, Net Rs Cr, Net US$ mn), plus the USD/INR rate |
| `Routes_Monthly` | Each route's daily sum vs NSDL's month total for that route |
| `Fallbacks` | Days where the Total row stood in for Equity (none in 2001–2020) |

A snapshot is committed at `outputs/NSDL_FPI_Daily_Equity_2001-01_to_2020-01.xlsx`.

## Four-sheet route workbook (Total / Stock Exchange / Primary / Combined)

`--route-workbook` (default `output/nsdl_fpi_equity_by_route.xlsx`) writes a
second, formula-driven workbook. A recalculated snapshot is at
`outputs/NSDL_FPI_Equity_by_Route_2001-2020.xlsx`.

| # | Sheet | Contents |
|---|---|---|
| 1 | `Total` | Equity per reporting day, 2001-01 → 2020-01 (4,660 rows): the Equity row before Dec 2009, the Equity Sub-total after |
| 2 | `Stock Exchange` | The Stock Exchange equity route, Dec 2009 → Jan 2020 (2,446 rows) |
| 3 | `Primary` | The Primary market & others equity route, same dates |
| 4 | `Combined` | **Formulas only:** links to `Total`; MATCH/INDEX lookups into the two route sheets by date; SE + Primary vs Total with an OK/CHECK flag against an editable tolerance (`T2`); SUMIFS summaries by calendar year and by FY |

Every sheet has an **FY** column for the Indian financial year (April–March),
e.g. `FY 2009-10`. It is a formula on the date, so it never goes stale. All
data sheets share one column layout (A Date, B Year, C Month, D FY, E
Basis/Route, F–I values, J USD/INR), so each `Combined` formula reads the same
column letter from each sheet. Recalculated in LibreOffice: 117,110 formulas,
0 errors, and all 2,446 split days are within 0.1 crore.

openpyxl writes formulas without cached results, so the workbook is flagged to
recalculate on open. Excel fills it in immediately. To give previewers values
too, recalculate it once in LibreOffice/Excel, as was done for the snapshot.
Six reporting dates in 2004–2006 fall on Saturdays. They are kept as NSDL
reports them.
