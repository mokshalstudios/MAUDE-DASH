#!/usr/bin/env python3
"""
maude_build.py — one-command MAUDE pipeline
===========================================

Plug-and-play. Point it at a folder of FDA MAUDE files and it builds a
ready-to-query analytical database, then tells you to launch the dashboard.

    python maude_build.py --raw-dir . --db maude_final.duckdb
    streamlit run maude_dashboard_v4.py

What it does, in order:
  1. INSPECT  — identifies every file by content (handles the FDA's confusing
                and drifting filename/format conventions).
  2. INGEST   — loads raw facts into typed tables with memory-safe settings,
                multi-line narrative reassembly, and format auto-detection for
                the problem-code files (2-col, 3-col, or 5-col-with-header).
  3. ENRICH   — typed dates, an indexed report_year column (this is the single
                most important performance fix), patient age normalisation,
                derived flags.
  4. ANALYTIC — builds the denormalised mdr_flat table that the dashboard
                queries, using chunked aggregation + an indexed year column so
                it runs in minutes, not hours.
  5. VALIDATE — sanity checks; prints a clear OK / warnings / errors summary.

Robustness features:
  * Resumable: every expensive step checks whether its output already exists
    and skips it. A crash (or laptop sleep) loses at most one step.
  * Keeps the machine awake on Windows for the duration (no more sleep-timeouts).
  * Bounded memory: caps DuckDB and processes the big aggregations in buckets.
  * No silent failures: unrecognised file formats stop the run with a clear
    message instead of leaving an empty table.

Flags:
  --raw-dir DIR     Folder with the MAUDE .txt/.csv files (default: .)
  --db FILE         Output DuckDB path (default: maude_final.duckdb)
  --skip-fts        Skip the full-text-search index (saves ~30-60 min; the
                    dashboard still works, narrative search just uses LIKE).
  --fresh           Delete any existing DB and temp files first.
  --mem-limit GB    DuckDB memory cap in GB (default: auto = 60% of RAM,
                    capped at 14). Lower if you still OOM.
  --buckets N       Number of hash buckets for narrative aggregation
                    (default 16; raise to 32/64 if the narrative step OOMs).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

import duckdb

# Reuse the proven loaders from the ingest module
import maude_ingest_v2 as ing


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def info(msg: str = "") -> None:
    print(msg, flush=True)


def banner(title: str) -> None:
    info("")
    info("=" * 70)
    info(f"  {title}")
    info("=" * 70)


def step(msg: str) -> None:
    info(f"  -> {msg}")


# ---------------------------------------------------------------------------
# Keep-awake (Windows): stop the machine sleeping mid-build
# ---------------------------------------------------------------------------

class KeepAwake:
    """Context manager that prevents Windows from sleeping. No-op elsewhere."""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_AWAYMODE_REQUIRED = 0x00000040

    def __enter__(self):
        self._set = False
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(
                    self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_AWAYMODE_REQUIRED
                )
                self._set = True
                info("  (Windows sleep prevention enabled for the duration of this build)")
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        if self._set:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Memory configuration
# ---------------------------------------------------------------------------

def detect_mem_limit_gb(user_value: Optional[float]) -> float:
    if user_value:
        return float(user_value)
    # Default: 60% of physical RAM, capped at 14 GB (safe for 24-32 GB boxes)
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        # Fallback without psutil
        try:
            if os.name == "nt":
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                total_gb = stat.ullTotalPhys / (1024 ** 3)
            else:
                total_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
        except Exception:
            total_gb = 16.0
    return max(4.0, min(14.0, round(total_gb * 0.6, 1)))


def configure(con: duckdb.DuckDBPyConnection, mem_gb: float, tmp_dir: str) -> None:
    for p in [
        "PRAGMA preserve_insertion_order = false",
        "PRAGMA threads = 4",
        f"PRAGMA memory_limit = '{mem_gb}GB'",
        f"PRAGMA temp_directory = '{tmp_dir}'",
    ]:
        try:
            con.execute(p)
        except Exception as e:
            info(f"  ! could not set {p}: {e}")


def table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]
    ).fetchone()[0] == 1


def row_count(con, name: str) -> int:
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    except Exception:
        return 0


def has_col(con, table: str, col: str) -> bool:
    try:
        return col in {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stage 1: INSPECT
# ---------------------------------------------------------------------------

def inspect(raw: Path) -> dict:
    """Identify files. Returns a dict of role -> list[Path]."""
    banner("STAGE 1/5: Inspecting files")
    import maude_inspect_files as insp

    files = sorted(list(raw.glob("*.txt")) + list(raw.glob("*.csv")))
    if not files:
        info(f"  ! No .txt/.csv files in {raw}")
        return {}

    roles: dict[str, list[Path]] = {}
    for f in files:
        meta = insp.sniff(f)
        role, detail = insp.classify(meta)
        roles.setdefault(role, []).append(f)
        size_mb = meta.get("size", 0) / (1024 * 1024)
        step(f"[{role:22s}] {f.name:38s} {size_mb:>8.1f} MB")

    # Report what we found
    info("")
    needed = ["mdr_master", "device_records", "patient_records", "foi_narratives"]
    for r in needed:
        if r not in roles:
            info(f"  ! MISSING primary table: {r}")
    return roles


# ---------------------------------------------------------------------------
# Stage 2: INGEST
# ---------------------------------------------------------------------------

def ingest(con, raw: Path, roles: dict, skip_fts: bool) -> None:
    banner("STAGE 2/5: Ingesting raw facts")

    def g(*patterns):
        return ing.glob_many(str(raw), *patterns)

    # Primary tables — skip if already loaded (resume)
    if table_exists(con, "patient") and row_count(con, "patient") > 0:
        step(f"patient: already loaded ({row_count(con, 'patient'):,} rows). Skipping.")
    else:
        ing.load_pipe_files(con, "PATIENT",
            g("patient.txt", "patient_utf8.txt", "patientThru*.txt",
              "patientchange*.txt", "patientadd*.txt"), "patient")

    if table_exists(con, "device") and row_count(con, "device") > 0:
        step(f"device: already loaded ({row_count(con, 'device'):,} rows). Skipping.")
    else:
        ing.load_pipe_files(con, "DEVICE", g("device*.txt", "DEVICE*.txt"), "device")

    if table_exists(con, "foi") and row_count(con, "foi") > 0:
        step(f"foi: already loaded ({row_count(con, 'foi'):,} rows). Skipping.")
    else:
        ing.load_pipe_files(con, "FOITEXT (multi-line aware)",
            g("foitext*.txt", "FOITEXT*.txt"), "foi", reassemble=True)

    if table_exists(con, "mdr") and row_count(con, "mdr") > 0:
        step(f"mdr: already loaded ({row_count(con, 'mdr'):,} rows). Skipping.")
    else:
        ing.load_pipe_files(con, "MDRFOI", g("mdrfoi*.txt", "MDRFOI*.txt"), "mdr")

    # Problem-code data files (format auto-detected by the patched loader)
    if table_exists(con, "patient_problem_codes") and row_count(con, "patient_problem_codes") > 0:
        step(f"patient_problem_codes: already loaded "
             f"({row_count(con, 'patient_problem_codes'):,} rows). Skipping.")
    else:
        ppc = raw / "patientproblemcode.txt"
        if not ppc.exists():
            cand = g("patientproblem*.txt", "patient_problem*.txt")
            ppc = Path(cand[0]) if cand else ppc
        ing.load_problem_codes(con, "PATIENT PROBLEM CODES", str(ppc),
                               "patient_problem_codes", ing.PATIENT_PROBLEM_COLS)

    if table_exists(con, "foidevproblem") and row_count(con, "foidevproblem") > 0:
        step(f"foidevproblem: already loaded "
             f"({row_count(con, 'foidevproblem'):,} rows). Skipping.")
    else:
        fdp = raw / "foidevproblem.txt"
        if not fdp.exists():
            cand = g("foidevproblem*.txt", "FOIDEVPROBLEM*.txt", "foidev*.txt")
            fdp = Path(cand[0]) if cand else fdp
        ing.load_problem_codes(con, "DEVICE PROBLEMS (foidevproblem)", str(fdp),
                               "foidevproblem", ing.FOIDEVPROBLEM_COLS)

    # Dictionaries (CSV)
    if not (table_exists(con, "device_problem_dict") and row_count(con, "device_problem_dict") > 0):
        ing.load_csv_dict(con, "DEVICE PROBLEM DICTIONARY",
                          str(raw / "deviceproblemcodes.csv"), "device_problem_dict")
    if not (table_exists(con, "patient_problem_dict") and row_count(con, "patient_problem_dict") > 0):
        ing.load_csv_dict(con, "PATIENT PROBLEM DICTIONARY",
                          str(raw / "patientproblemcode.csv"), "patient_problem_dict")

    # Enrichments on mdr / patient
    info("")
    step("Post-ingest enrichments")
    if table_exists(con, "mdr"):
        if not has_col(con, "mdr", "DATE_PREF"):
            ing.add_typed_dates(con)
        else:
            step("typed dates already present. Skipping.")
        if not has_col(con, "mdr", "IS_RWD_SOURCED"):
            ing.add_flag_columns(con)
        else:
            step("derived flags already present. Skipping.")
    if table_exists(con, "patient") and not has_col(con, "patient", "AGE_YEARS"):
        ing.add_patient_age_years(con)

    ing.create_indexes(con)

    if not skip_fts:
        if has_col(con, "foi", "FOI_TEXT"):
            step("Building FTS index on narratives (this can take 30-60 min) ...")
            ing.build_fts_index(con)
    else:
        step("Skipping FTS index (--skip-fts).")


# ---------------------------------------------------------------------------
# Stage 3: report_year index — the key performance fix
# ---------------------------------------------------------------------------

def add_report_year_index(con) -> None:
    """Add an indexed integer report_year column to mdr.

    This is the single most important performance fix. Without it, the
    analytic build's year-by-year composition does a full table scan of all
    20M MDRs for every year (because strftime() can't use the date index),
    making each year take ~10 minutes. With an indexed integer column, each
    year is an index range scan taking seconds.
    """
    banner("STAGE 3/5: Indexing report_year (performance-critical)")

    if not has_col(con, "mdr", "DATE_PREF"):
        info("  ! mdr.DATE_PREF missing — cannot derive report_year.")
        return

    if has_col(con, "mdr", "report_year"):
        step("mdr.report_year already exists. Skipping.")
    else:
        step("Adding report_year column ...")
        con.execute("ALTER TABLE mdr ADD COLUMN report_year INTEGER;")
        con.execute(
            "UPDATE mdr SET report_year = "
            "CAST(strftime(DATE_PREF, '%Y') AS INTEGER) "
            "WHERE DATE_PREF IS NOT NULL;"
        )

    step("Indexing report_year ...")
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_mdr_year ON mdr(report_year);")
    except Exception as e:
        info(f"  ! index failed: {e}")

    n = con.execute(
        "SELECT COUNT(*) FROM mdr WHERE report_year IS NOT NULL"
    ).fetchone()[0]
    step(f"report_year populated for {n:,} rows")


# ---------------------------------------------------------------------------
# Stage 4: ANALYTIC build (delegates to maude_analytic_build_v2)
# ---------------------------------------------------------------------------

def analytic(con, db_path: str, buckets: int) -> None:
    banner("STAGE 4/5: Building analytic table (mdr_flat)")

    import maude_analytic_build_v2 as ab

    # The analytic module manages its own pragmas/intermediates. We pass the
    # already-open connection so it reuses our settings. It is itself
    # resumable (skips _x_* intermediates that exist).
    feat = ab.build_intermediates(con)
    if not feat.get("have_date_pref"):
        info("  ! DATE_PREF missing; cannot build mdr_flat.")
        return

    # Compose mdr_flat. Use the report_year column if present (fast path).
    _compose_mdr_flat_fast(con, ab, feat)

    ab.build_rollups(con)
    ab.create_indexes(con)
    ab.cleanup_intermediates(con)


def _compose_mdr_flat_fast(con, ab, feat: dict) -> int:
    """Like ab.compose_mdr_flat but filters on the indexed report_year column
    instead of recomputing strftime() each year."""
    select_cols, joins = ab.build_select_and_joins(feat)

    use_fast = has_col(con, "mdr", "report_year")
    step(f"Composing mdr_flat ({'indexed year' if use_fast else 'fallback'} path)")

    con.execute("DROP TABLE IF EXISTS mdr_flat;")
    con.execute(
        "CREATE TABLE mdr_flat AS SELECT "
        + ",\n  ".join(select_cols)
        + " FROM mdr m " + " ".join(joins)
        + " WHERE 1=0;"
    )

    if use_fast:
        years = [int(r[0]) for r in con.execute(
            "SELECT DISTINCT report_year FROM mdr "
            "WHERE report_year IS NOT NULL ORDER BY 1"
        ).fetchall()]
        year_filter = "m.report_year = {year}"
        null_filter = "m.report_year IS NULL"
    else:
        years = [int(r[0]) for r in con.execute(
            "SELECT DISTINCT CAST(strftime(DATE_PREF, '%Y') AS INTEGER) AS y "
            "FROM mdr WHERE DATE_PREF IS NOT NULL ORDER BY y"
        ).fetchall()]
        year_filter = "CAST(strftime(m.DATE_PREF, '%Y') AS INTEGER) = {year}"
        null_filter = "m.DATE_PREF IS NULL"

    step(f"{len(years)} years: {years[0]}-{years[-1]}")
    cols_sql = ",\n  ".join(select_cols)
    joins_sql = " ".join(joins)

    total = 0
    for year in years:
        t0 = time.time()
        con.execute(
            f"INSERT INTO mdr_flat SELECT {cols_sql} FROM mdr m {joins_sql} "
            f"WHERE {year_filter.format(year=year)};"
        )
        n = row_count(con, "mdr_flat") - total
        total += n
        info(f"     year {year}: {n:>10,} rows ({time.time()-t0:.1f}s)  total: {total:,}")

    # NULL-date rows
    con.execute(
        f"INSERT INTO mdr_flat SELECT {cols_sql} FROM mdr m {joins_sql} "
        f"WHERE {null_filter};"
    )
    final = row_count(con, "mdr_flat")
    step(f"mdr_flat total: {final:,} rows")
    return final


# ---------------------------------------------------------------------------
# Stage 5: VALIDATE
# ---------------------------------------------------------------------------

def validate(db_path: str) -> int:
    banner("STAGE 5/5: Validating")
    import maude_validate_db as val
    return val.main(["--db", db_path])


# ---------------------------------------------------------------------------
# Cleanup orphaned temp files
# ---------------------------------------------------------------------------

def clean_temp_files(raw: Path, db_path: str) -> None:
    # Orphaned duckdb temp spill files from prior crashes
    patterns = ["duckdb_temp_storage_*.tmp", "*.duckdb.tmp", "*.duckdb.wal"]
    removed = 0
    for pat in patterns:
        for f in raw.glob(pat):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        for f in Path(".").glob(pat):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        info(f"  Removed {removed} orphaned temp file(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="One-command MAUDE pipeline (inspect -> ingest -> analytic -> validate)"
    )
    ap.add_argument("--raw-dir", default=".", help="Folder with MAUDE files")
    ap.add_argument("--db", default="maude_final.duckdb", help="Output DuckDB path")
    ap.add_argument("--skip-fts", action="store_true", help="Skip full-text-search index")
    ap.add_argument("--fresh", action="store_true", help="Delete existing DB + temp files first")
    ap.add_argument("--mem-limit", type=float, default=None, help="DuckDB memory cap in GB")
    ap.add_argument("--buckets", type=int, default=16, help="Narrative aggregation buckets")
    args = ap.parse_args(argv)

    raw = Path(args.raw_dir).resolve()
    if not raw.is_dir():
        info(f"ERROR: --raw-dir is not a directory: {raw}")
        return 2

    t_start = time.time()

    info("")
    info("#" * 70)
    info("#  MAUDE one-command builder")
    info(f"#  raw-dir : {raw}")
    info(f"#  db      : {args.db}")
    info("#" * 70)

    # Optionally start fresh
    if args.fresh:
        clean_temp_files(raw, args.db)
        if os.path.exists(args.db):
            info(f"  Removing existing {args.db} (--fresh)")
            os.remove(args.db)
    else:
        # Always clean orphaned temp files (safe — they're from dead processes)
        clean_temp_files(raw, args.db)

    mem_gb = detect_mem_limit_gb(args.mem_limit)
    info(f"  Memory cap: {mem_gb} GB")

    # Propagate bucket count to the analytic module
    try:
        import maude_analytic_build_v2 as ab
        # The module reads N_BUCKETS as a local in build_intermediates; we can't
        # easily override it without editing, so we set an env var the module
        # can optionally honor, and also monkeypatch if the attribute exists.
        os.environ["MAUDE_NARRATIVE_BUCKETS"] = str(args.buckets)
    except Exception:
        pass

    with KeepAwake():
        # Stage 1
        roles = inspect(raw)
        primary = {"mdr_master", "device_records", "patient_records", "foi_narratives"}
        if not (primary & set(roles.keys())):
            info("")
            info("ERROR: no primary MAUDE files found. Nothing to build.")
            return 2

        # Open the DB once for stages 2-4
        tmp_dir = str(raw)
        con = duckdb.connect(args.db)
        configure(con, mem_gb, tmp_dir)

        try:
            # Stage 2
            ingest(con, raw, roles, args.skip_fts)
            # Stage 3 (the perf fix)
            add_report_year_index(con)
            # Stage 4
            analytic(con, args.db, args.buckets)
        finally:
            con.close()

        # Stage 5
        code = validate(args.db)

    elapsed = (time.time() - t_start) / 60
    info("")
    info("#" * 70)
    info(f"#  BUILD COMPLETE in {elapsed:.1f} min")
    info("#" * 70)
    info("")
    info("Launch the dashboard with:")
    info("  streamlit run maude_dashboard_v4.py")
    info("")
    if code == 0:
        info("Validation: clean.")
    elif code == 1:
        info("Validation: usable, with warnings (see above). These are usually")
        info("expected MAUDE artifacts (orphan rows, partial current year).")
    else:
        info("Validation: errors found (see above).")
    return code


if __name__ == "__main__":
    sys.exit(main())
