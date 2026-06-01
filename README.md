# MAUDE-Dash

An open-source, reproducible pipeline and interactive dashboard for analyzing
the U.S. FDA **Manufacturer and User Facility Device Experience (MAUDE)**
database — the FDA's repository of medical device adverse-event reports.

MAUDE-Dash turns the FDA's large, awkward, pipe-delimited text files into a
fast local analytical database and a publication-grade Streamlit dashboard with
proper statistics: Wilson confidence intervals, disproportionality analysis
(PRR/ROR), trend tests, subgroup forest plots, and the official FDA seven-harm
clinical-outcome classification (21 CFR 803.3).

This is an expanded successor to the tool described in Porwal,
*Surgical Neurology International*, 2026 (DOI: 10.25259/SNI_201_2026).

---

## Why this exists

MAUDE is a cornerstone of post-market device surveillance, but it is hard to
use: tens of millions of records spread across many pipe-delimited files,
inconsistent encodings, multi-line narrative fields, format drift between
years, and a relational structure that has to be reassembled before any
analysis. MAUDE-Dash handles all of that and gives clinicians and researchers
an interactive environment for signal exploration and hypothesis generation —
without writing data-engineering code.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Download MAUDE files from the FDA and unzip them all into one folder:
#    https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files

# 3. Build the database (one command — inspect, ingest, index, analytic, validate)
python maude_build.py --raw-dir /path/to/maude_files --db maude_final.duckdb

# 4. Launch the dashboard
streamlit run maude_dashboard_v4.py
```

On Windows, if your files are in the current folder:

```powershell
python maude_build.py --raw-dir . --db maude_final.duckdb
streamlit run maude_dashboard_v4.py
```

First build on a full ~10-year corpus takes roughly 30-90 minutes depending on
hardware and whether you build the full-text-search index. Add `--skip-fts` to
skip it (saves 30-60 minutes; narrative search then uses `LIKE`).

---

## What `maude_build.py` does

A single command runs the whole pipeline, with progress logging at each stage:

1. **Inspect** — identifies every file by *content*, not filename. MAUDE's
   filenames are inconsistent and the FDA documentation is stale; this step
   correctly distinguishes per-report data files from dictionary files and
   detects format variants.
2. **Ingest** — loads raw facts into typed tables. Multi-line narratives are
   reassembled with a state machine; problem-code files are format-auto-detected
   (2-column headerless, 3-column headerless, or the current 5-column-with-header
   variant); encodings and byte-order-marks are normalized.
3. **Index `report_year`** — adds an indexed integer year column to the master
   table. This is a critical performance step that turns the analytic build's
   per-year work from full table scans into index lookups.
4. **Analytic build** — constructs the denormalized `mdr_flat` table (one row
   per report, with patient, device, problem-code, narrative, and outcome
   fields joined in) plus pre-aggregated rollup tables. Memory-safe: the large
   narrative aggregation runs in hash buckets and the table is composed year by
   year.
5. **Validate** — sanity checks the result and prints a clear OK / warnings /
   errors summary.

**Built-in robustness:**

- **Resumable** — every expensive step checks whether its output already exists
  and skips it. A crash or a laptop going to sleep loses at most one step; just
  re-run the same command.
- **Keeps Windows awake** for the duration of the build.
- **Bounded memory** — auto-caps DuckDB at 60% of RAM (max 14 GB by default) and
  processes the giant narrative aggregation in buckets. Tune with `--mem-limit`
  and `--buckets` if needed.
- **No silent failures** — an unrecognized file format stops the run with a
  clear message instead of leaving an empty table.

Flags:

```
--raw-dir DIR     Folder with MAUDE .txt/.csv files (default: current folder)
--db FILE         Output DuckDB path (default: maude_final.duckdb)
--skip-fts        Skip the full-text-search index (faster; LIKE search still works)
--fresh           Delete any existing DB + temp files and start clean
--mem-limit GB    DuckDB memory cap in GB (default: auto)
--buckets N       Narrative-aggregation hash buckets (default 16; raise if OOM)
```

---

## Dashboard tabs

The Streamlit dashboard (`maude_dashboard_v4.py`) provides 23 analytical views.
All filtering happens in the sidebar (product code, manufacturer, device terms,
date range, narrative search, event types, and cohort exclusions). Every tab's
data is exportable to Excel.

**Descriptive**
- **Preview** — matching reports with key fields
- **Yearly Trends** — report volume over time
- **Event Trends** — death / injury / malfunction over time
- **Demographics** — patient age and sex distributions
- **Reporter / Source** — who filed the reports
- **Geography** — reports by reporter country (choropleth)
- **Manufacturer Mix** — concentration with Herfindahl-Hirschman index

**Clinical**
- **Clinical Outcomes** — the FDA seven-harm classification (death,
  life-threatening, hospitalization, disability, congenital anomaly, required
  intervention, other) with Wilson 95% confidence intervals
- **Problem Codes** — top device and patient problem terms
- **Problem → Outcome** — outcome severity stratified by problem code (which
  failure modes carry the worst clinical consequences), with Wilson CIs
- **Device Age at Failure** — time-to-failure distribution and cumulative
  incidence
- **Death Deep-Dive** — full detail on death reports

**Statistical**
- **Disproportionality** — PRR and ROR with 95% CIs, Yates chi-square, Fisher's
  exact for small cells, and EMA-2008 signal flagging
- **Subgroup Analysis** — outcome rates by sex / age band / year / source /
  country / occupation as a forest plot, with a chi-square test across subgroups
- **Trend Tests** — Cochran-Armitage trend test and Mann-Kendall non-parametric
  trend test on yearly counts
- **Cohort Comparison** — compare two cohorts with a chi-square test
- **Sensitivity Analysis** — headline numbers recomputed under several exclusion
  scenarios side by side
- **Time-to-Report** — reporting-lag distribution

**Text & reproducibility**
- **Narratives** — n-gram frequency analysis and word cloud over event
  descriptions
- **Raw Narratives** — full narrative text for manual review
- **STROBE Report** — auto-generated methods paragraph matching the exact filter
  applied, plus the SQL for reproducibility
- **Master Export** — denormalized analysis-ready dataset (Excel up to 200k rows,
  or CSV for the full set)

---

## Statistical methods

The statistics live in `maude_stats.py`, an independent, unit-tested module:

| Method | Use |
|---|---|
| Wilson score interval | 95% CIs for all proportions (robust at small n / extreme p) |
| Proportional Reporting Ratio (PRR) | disproportionality, log-normal 95% CI |
| Reporting Odds Ratio (ROR) | disproportionality, log-normal 95% CI |
| Yates-corrected chi-square | 2×2 association test |
| Fisher's exact test | 2×2 with any expected cell < 10 (via SciPy) |
| Chi-square test of independence | k×c contingency tables |
| Cochran-Armitage test | trend in a proportion across ordered years |
| Mann-Kendall test | monotonic trend in a time series |
| EMA-2008 rule | exploratory signal flag (PRR ≥ 2, χ² ≥ 4, a ≥ 3) |

Wilson CIs and the ratio statistics are validated against `statsmodels`
reference values in the test suite.

**Important interpretive caveat.** MAUDE is a passive surveillance system with
no defined denominator of exposed devices. Reporting is subject to
under-reporting, stimulated/“notoriety” reporting, and selection effects.
Disproportionality signals and outcome rates are **hypothesis-generating only**
and do not establish causation or incidence. The dashboard surfaces these
limitations (data-quality tab, sensitivity analysis, STROBE methods text) rather
than hiding them.

---

## File overview

| File | Role |
|---|---|
| `maude_build.py` | **One-command pipeline** (inspect → ingest → index → analytic → validate). Start here. |
| `maude_ingest_v2.py` | Raw-fact ingestion: typed tables, narrative reassembly, problem-code format auto-detection. |
| `maude_analytic_build_v2.py` | Builds the denormalized `mdr_flat` table and rollups (memory-safe, resumable). |
| `maude_dashboard_v4.py` | The Streamlit dashboard. |
| `maude_stats.py` | Statistical methods module (independent, unit-tested). |
| `maude_validate_db.py` | Database sanity checker (exit codes 0/1/2 for CI). |
| `maude_inspect_files.py` | Content-based file identifier (run it if a build behaves oddly). |
| `maude_load_patient_problems.py` | Targeted loader to add/fix the patient-problem-codes table on an existing DB without a full rebuild. |
| `test_maude.py` | Test suite (26 tests: statistics, ingest, analytic build, dashboard SQL). |
| `requirements.txt` | Dependencies. |

---

## Testing

```bash
python test_maude.py
```

26 tests covering the statistics module (validated against statsmodels), the
ingest pipeline (including multi-line narrative reassembly, CSV dictionary
loading, the 5-column-with-header patient-problem format, and patient-age
normalization), the analytic build (outcome decoding, device-age parsing,
supplement detection), and the dashboard's SQL paths.

After building a real database, validate it:

```bash
python maude_validate_db.py --db maude_final.duckdb
```

### Expected validator warnings (harmless)

- **Referential integrity** (orphan rows in dependent tables): the FDA's
  dependent files contain historical records for reports no longer in the master
  file. The dashboard always joins *from* `mdr_flat`, so these are invisible to
  analyses.
- **Partial current year**: the latest year is incomplete if the data was
  downloaded mid-year. Exclude it from rate comparisons.

---

## A note on MAUDE file formats

The FDA's file naming and documentation are inconsistent and have drifted over
time. Two specific gotchas this pipeline handles for you:

1. **The `foidevproblem` / `deviceproblemcodes` naming is effectively inverted**
   between the FDA's prose docs and the actual files. The pipeline identifies
   files by content, so this doesn't matter in practice — but it's why
   `maude_inspect_files.py` exists.
2. **`patientproblemcode.txt` is now a 5-column file with a header row**
   (`MDR_REPORT_KEY | PATIENT_SEQUENCE_NO | PROBLEM_CODE | DATE_ADDED |
   DATE_CHANGED`), not the 2-column headerless format older documentation
   describes. The ingest auto-detects this.

If a build ever produces an empty or surprising table, run:

```bash
python maude_inspect_files.py --raw-dir /path/to/maude_files
```

to see exactly what the pipeline detects in each file.

---

## Troubleshooting

**Out-of-memory during the build.** Lower the cap and raise the bucket count:
```bash
python maude_build.py --raw-dir . --db maude_final.duckdb --mem-limit 10 --buckets 32
```

**Build was interrupted (crash / sleep / Ctrl-C).** Just re-run the same
command — it resumes from where it stopped.

**Patient problems tab is empty but you have the file.** Your DB predates the
format fix. Either rebuild, or patch in place:
```bash
python maude_load_patient_problems.py --file patientproblemcode.txt --db maude_final.duckdb --yes
```

**A year takes many minutes during the analytic build.** You're likely running
an older copy without the `report_year` index step. Use `maude_build.py`, which
adds it automatically.

---

## License

MIT.

## Citation

If you use MAUDE-Dash in published work, please cite:

> Porwal M. Manufacturer and user facility device experience-dash: An
> open-source, interactive dashboard for real-time post-market surveillance of
> medical devices. *Surg Neurol Int.* 2026. DOI: 10.25259/SNI_201_2026
