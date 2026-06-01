# MAUDE Dashboard — Quick Start

## Your situation

You already have a 55 GB `maude_final.duckdb` with the primary tables and
patient problem codes loaded, but the crash left it without `mdr_flat` (the
table the dashboard needs) and some orphaned temp files.

You have two paths. **Path A is faster** because it reuses what's already built.

---

## Path A — finish the existing database (recommended, ~20-40 min)

Your DB already has `patient_problem_codes` (22.4M rows) and the 5 intermediate
`_x_*` aggregate tables. The new `maude_build.py` will detect those, skip them,
add the performance-critical `report_year` index, and build `mdr_flat` fast.

```powershell
cd "~\Downloads\MAUDE Analyzer"

# 1. Delete the orphaned temp files from the crash (they're from a dead process)
Remove-Item duckdb_temp_storage_*.tmp -ErrorAction SilentlyContinue

# 2. Run the unified builder. It resumes: skips loaded tables, adds the
#    report_year index, builds mdr_flat with the fast indexed-year path.
python maude_build.py --raw-dir . --db maude_final.duckdb --skip-fts

# 3. Launch
streamlit run maude_dashboard_v4.py
```

`--skip-fts` skips the full-text-search index. Use it if you don't already have
FTS built (it saves 30-60 min and the dashboard works fine without it — narrative
search just uses `LIKE` instead). If your earlier run already built FTS, you can
drop the flag, but skipping is harmless either way.

**Why this is now fast:** the earlier hang was because each year's slice of
`mdr_flat` did a full scan of all 20M rows (the `strftime()` year extraction
couldn't use an index). `maude_build.py` adds an indexed integer `report_year`
column to `mdr` first, turning each year into a sub-second index lookup. Years
go from ~10 minutes each to seconds.

---

## Path B — rebuild from scratch (~2-4 hours, only if Path A fails)

If the database is corrupted (Path A errors when opening it), rebuild clean:

```powershell
cd "~\Downloads\MAUDE Analyzer"
python maude_build.py --raw-dir . --db maude_final.duckdb --fresh --skip-fts
streamlit run maude_dashboard_v4.py
```

`--fresh` deletes the existing DB and temp files and starts over. This re-ingests
all 40M narrative rows etc., so it's slow, but it's fully hands-off — the
keep-awake feature stops your laptop sleeping mid-run.

---

## What `maude_build.py` does

One command runs the whole pipeline:

1. **Inspect** — identifies every file by content (handles the FDA's confusing
   filenames and the 5-column-with-header `patientproblemcode.txt` format that
   silently failed before).
2. **Ingest** — loads raw facts; multi-line narrative reassembly; problem-code
   format auto-detection; memory-safe settings from the start.
3. **Index report_year** — the performance fix.
4. **Analytic** — builds `mdr_flat` (chunked narrative aggregation + indexed
   year loop) plus all the rollup tables.
5. **Validate** — prints a clear OK/warning/error summary.

Built-in robustness:
- **Resumable** — every step checks if its output exists and skips it. A crash
  or laptop-sleep loses at most one step. Just re-run the same command.
- **Keeps Windows awake** for the whole build (no more sleep-timeouts).
- **Bounded memory** — auto-caps DuckDB at 60% of RAM (max 14 GB) and processes
  the giant narrative aggregation in 16 buckets. If you still OOM, add
  `--mem-limit 10` or `--buckets 32`.
- **No silent failures** — an unrecognized file format stops with a clear
  message instead of leaving an empty table.

Flags:
```
--raw-dir DIR     Folder with MAUDE files (default: current folder)
--db FILE         Output path (default: maude_final.duckdb)
--skip-fts        Skip full-text-search index (faster; LIKE search still works)
--fresh           Delete existing DB + temp files, start clean
--mem-limit GB    DuckDB memory cap (default: auto)
--buckets N       Narrative aggregation buckets (default: 16; raise if OOM)
```

---

## Expected validator warnings (harmless)

The validator may report a few warnings — these are normal MAUDE artifacts,
not problems:

- **Referential integrity** (~10-17% orphan rows in `patient`, `foidevproblem`,
  etc.): the FDA's dependent files contain historical records for MDRs they no
  longer redistribute in the master file. The dashboard always joins *from*
  `mdr_flat`, so these are invisible to your analyses.
- **Partial current year**: the most recent year is incomplete because the data
  was downloaded mid-year. Exclude it from rate comparisons.

A clean run ends with `Database looks good.` or `usable, with warnings`.

---

## After it builds

```powershell
streamlit run maude_dashboard_v4.py
```

For your first publication run, reproduce the pedicle-screw analysis from your
paper: in the sidebar set Product Code to `KWP` (or `MNI`), date range 2018-2023.
The Clinical Outcomes tab will now show the FDA 7-harm classification with Wilson
95% CIs — the figure the original MAUDE-Dash couldn't produce.

---

## If something goes wrong

Run the inspector to see exactly what the builder sees:

```powershell
python maude_inspect_files.py --raw-dir .
```

Run the test suite to confirm the code itself is sound:

```powershell
python test_maude.py
```

25 tests should pass. If they do but your real build fails, the issue is
data-specific and the error message will say which file/step.
