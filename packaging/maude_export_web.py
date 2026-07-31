"""
maude_export_web.py — build the MaudeDash web tier
==================================================

Collapses the full ~73 GB `maude_final.duckdb` build into a set of static,
range-request-friendly Parquet files that a browser can query directly with
DuckDB-WASM. No server-side process, no database daemon: the output of this
script is a folder of files that any static host (DreamHost shared included)
can serve as-is.

Why this is ~40x smaller than the source database
-------------------------------------------------
The 73 GB is dominated by things a reader never queries:

  terms/docs/dict/...  1.54 billion rows of DuckDB FTS index   (dropped —
                       narrative search runs as a scan over the cohort instead)
  mdr, device, patient raw staging tables, already denormalised into mdr_flat
                       (dropped)
  *_l columns          lowercased duplicates of five text columns, including a
                       second full copy of every narrative (dropped — queries
                       use lower()/ILIKE at read time)

What survives is `mdr_flat` itself (one row per MDR_REPORT_KEY, all 4 000-char
narratives intact), the two problem-code bridge tables, the code dictionaries,
and the precomputed rollups. That is full analytic parity with the desktop app.

Layout produced
---------------
    <out>/manifest.json          data vintage, row counts, file list, checksums
    <out>/summary.json           corpus-wide figures for instant landing render
    <out>/mdr_pre2014.parquet    1991-2013 + undated reports
    <out>/mdr_2014.parquet ...   one file per year 2014..latest
    <out>/devprob.parquet        MDR_REPORT_KEY -> device problem code
    <out>/patprob.parquet        MDR_REPORT_KEY -> patient problem code
    <out>/dict_device.parquet    FDA_CODE -> TERM
    <out>/dict_patient.parquet
    <out>/agg_*.parquet          precomputed rollups
    <out>/foi_*.parquet          OPTIONAL (--include-foi): untruncated
                                 multi-part narratives, +~2.2 GB

Each mdr_*.parquet is sorted by product_code so Parquet row-group min/max
statistics let DuckDB-WASM skip most of the file for the single most common
filter in the tool. Row groups are kept small (50 000 rows) so that a column
chunk is a cheap HTTP range request rather than a multi-megabyte download.

Usage
-----
    python maude_export_web.py --db maude_final.duckdb --out ../web/data
    python maude_export_web.py --db maude_final.duckdb --out ../web/data \
        --no-narratives            # structured only, ~700 MB
    python maude_export_web.py --db maude_final.duckdb --out ../web/data \
        --include-foi              # + untruncated narratives, +~2.2 GB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

import duckdb

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

# Everything on mdr_flat except the five redundant lowercased duplicates
# (brand_name_l, generic_name_l, model_number_l, manufacturer_l,
# narrative_desc_l). Dropping narrative_desc_l alone removes a second full
# copy of every narrative in the corpus.
STRUCTURAL_COLS = [
    "MDR_REPORT_KEY", "REPORT_NUMBER", "EVENT_TYPE",
    "DATE_RECEIVED_D", "DATE_OF_EVENT_D", "DATE_PREF",
    "report_year", "report_month", "lag_days",
    "SOURCE_TYPE", "REPORTER_OCCUPATION_CODE",
    "IS_RWD_SOURCED", "IS_FORWARDED_803_22_B2",
    "HAS_REDACTION_B4", "HAS_REDACTION_B6",
    "reporter_country_code",
    "is_supplement", "supplement_number", "initial_report",
    "adverse_event_flag", "product_problem_flag",
    "BRAND_NAME", "GENERIC_NAME", "MODEL_NUMBER", "manufacturer", "product_code",
    "device_count", "device_age_days", "device_age_text_raw",
    "implant_flag", "device_operator",
    "device_evaluated_by_manufacturer", "reprocessed_and_reused",
    "patient_count", "age_years_min", "age_years_max", "age_years_avg", "sex_list",
    "outcome_death", "outcome_life_threatening", "outcome_hospitalization",
    "outcome_disability", "outcome_congenital_anomaly",
    "outcome_required_intervention", "outcome_other", "any_serious_outcome",
    "outcome_codes_raw",
    "device_problem_codes", "patient_problem_codes",
    "has_narrative", "narr_part_count",
]

NARRATIVE_COLS = ["narrative_desc", "narrative_mfg"]

# Small row groups keep each column chunk a cheap HTTP range request.
ROW_GROUP_SIZE = 50_000
ZSTD_LEVEL = 9

# Years below this are sparse (all of 1991-2013 together is ~2.9M rows, less
# than a single recent year) so they ship as one file.
SPLIT_FROM_YEAR = 2014

# The FDA device, narrative and problem-code files begin here. Reports before
# it exist in the MDR master but carry no product code, manufacturer, brand,
# narrative or problem code, so they cannot participate in a disproportionality
# comparator. Must match CODE_ELIGIBLE_FROM_YEAR in web/assets/db.js.
CODE_ELIGIBLE_FROM_YEAR = 2015


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024.0:
            return f"{nbytes:,.1f} {unit}"
        nbytes /= 1024.0
    return f"{nbytes:,.1f} PB"


def sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def copy_to_parquet(con: duckdb.DuckDBPyConnection, select_sql: str, dest: str,
                    row_group_size: int = ROW_GROUP_SIZE) -> dict:
    """Run COPY ... TO parquet and return a manifest entry."""
    tmp = dest + ".partial"
    if os.path.exists(tmp):
        os.remove(tmp)
    t0 = time.time()
    con.execute(
        f"COPY ({select_sql}) TO '{tmp.replace(chr(39), chr(39) * 2)}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL {ZSTD_LEVEL}, "
        f"ROW_GROUP_SIZE {row_group_size})"
    )
    os.replace(tmp, dest)
    elapsed = time.time() - t0
    size = os.path.getsize(dest)
    rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{dest.replace(chr(39), chr(39) * 2)}')"
    ).fetchone()[0]
    print(f"    {os.path.basename(dest):<28} {rows:>12,} rows  "
          f"{human(size):>10}  {elapsed:5.0f}s")
    return {
        "file": os.path.basename(dest),
        "rows": int(rows),
        "bytes": int(size),
        "sha256": sha256_of(dest),
    }


def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]
    ).fetchone()[0])


def columns_of(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the MaudeDash static web tier from maude_final.duckdb")
    ap.add_argument("--db", default="maude_final.duckdb",
                    help="Source DuckDB built by maude_build.py")
    ap.add_argument("--out", default=os.path.join("..", "web", "data"),
                    help="Output folder for the static data files")
    ap.add_argument("--no-narratives", action="store_true",
                    help="Omit narrative text (~700 MB instead of ~1.8 GB). "
                         "Disables word cloud / phrase mining / narrative search.")
    ap.add_argument("--include-foi", action="store_true",
                    help="Also export untruncated multi-part narratives from "
                         "the foi table (+~2.2 GB). Only 0.33%% of parts exceed "
                         "the 4 000-char cap already present in mdr_flat.")
    ap.add_argument("--mem-limit", default="8GB",
                    help="DuckDB memory cap for the export (default 8GB)")
    ap.add_argument("--threads", type=int, default=0,
                    help="DuckDB threads (0 = DuckDB default)")
    ap.add_argument("--temp-dir", default=None,
                    help="Spill directory for the sort steps (needs ~10 GB free)")
    ap.add_argument("--clean", action="store_true",
                    help="Delete existing output files first")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2

    out = os.path.abspath(args.out)
    if args.clean and os.path.isdir(out):
        print(f"Cleaning {out}")
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    include_narr = not args.no_narratives

    print("=" * 72)
    print("MaudeDash web-tier export")
    print("=" * 72)
    print(f"  source     : {args.db}  ({human(os.path.getsize(args.db))})")
    print(f"  output     : {out}")
    print(f"  narratives : {'included (4 000-char)' if include_narr else 'EXCLUDED'}")
    print(f"  foi        : {'included (untruncated)' if args.include_foi else 'excluded'}")
    print()

    con = duckdb.connect(args.db, read_only=True)
    con.execute(f"SET memory_limit='{args.mem_limit}'")
    if args.threads:
        con.execute(f"SET threads={args.threads}")
    if args.temp_dir:
        os.makedirs(args.temp_dir, exist_ok=True)
        con.execute(f"SET temp_directory='{args.temp_dir}'")

    if not table_exists(con, "mdr_flat"):
        print("ERROR: mdr_flat is missing. Run maude_build.py first.", file=sys.stderr)
        return 2

    present = columns_of(con, "mdr_flat")
    cols = [c for c in STRUCTURAL_COLS if c in present]
    missing = [c for c in STRUCTURAL_COLS if c not in present]
    if missing:
        print(f"  NOTE: {len(missing)} expected column(s) absent from mdr_flat "
              f"and will be skipped: {', '.join(missing)}")
    if include_narr:
        cols += [c for c in NARRATIVE_COLS if c in present]
    col_sql = ", ".join(f'"{c}"' for c in cols)

    manifest: dict = {
        "product": "MaudeDash",
        "tier": "web",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_db_bytes": os.path.getsize(args.db),
        "narratives_included": include_narr,
        "narrative_char_cap": 4000 if include_narr else None,
        "foi_included": bool(args.include_foi),
        "columns": cols,
        "row_group_size": ROW_GROUP_SIZE,
        "files": {},
    }

    # ---------------------------------------------------------------- years
    print("  [1/5] mdr_flat, partitioned by year, sorted by product_code")
    years = [r[0] for r in con.execute(
        "SELECT DISTINCT report_year FROM mdr_flat "
        "WHERE report_year IS NOT NULL ORDER BY 1").fetchall()]
    split_years = [y for y in years if y >= SPLIT_FROM_YEAR]

    mdr_files = []
    # Sparse early years plus undated reports in one file.
    entry = copy_to_parquet(con, f"""
        SELECT {col_sql} FROM mdr_flat
        WHERE report_year IS NULL OR report_year < {SPLIT_FROM_YEAR}
        ORDER BY product_code NULLS LAST, MDR_REPORT_KEY
    """, os.path.join(out, "mdr_pre2014.parquet"))
    entry["year_min"] = int(min(years)) if years else None
    entry["year_max"] = SPLIT_FROM_YEAR - 1
    mdr_files.append(entry)

    for y in split_years:
        entry = copy_to_parquet(con, f"""
            SELECT {col_sql} FROM mdr_flat
            WHERE report_year = {int(y)}
            ORDER BY product_code NULLS LAST, MDR_REPORT_KEY
        """, os.path.join(out, f"mdr_{int(y)}.parquet"))
        entry["year_min"] = entry["year_max"] = int(y)
        mdr_files.append(entry)
    manifest["files"]["mdr"] = mdr_files

    # ------------------------------------------------------ problem bridges
    #
    # These drive the disproportionality screen, which was by far the slowest
    # panel: 34 seconds against the original layout, versus under a second for
    # everything else. Three properties of the data make it much cheaper.
    #
    #   1. (MDR_REPORT_KEY, code) pairs are already unique — verified, zero
    #      duplicates in 20,042,625 rows — so the cohort side can use COUNT(*)
    #      instead of COUNT(DISTINCT) over 20M string keys.
    #   2. MDR_REPORT_KEY is entirely numeric and never exceeds 21,050,631, so
    #      it fits a 4-byte UINTEGER instead of an 8-character VARCHAR. Narrow
    #      fixed-width keys hash and join far faster in WebAssembly.
    #   3. Carrying product_code and report_year on the bridge, with the file
    #      sorted by product_code, lets Parquet row-group statistics skip
    #      almost the whole file for the overwhelmingly common cohort shape
    #      (one product code over a year range). Both columns are low
    #      cardinality and sorted, so they cost very little space.
    print("  [2/5] problem-code bridge tables, keyed and sorted for pruning")
    bridges = {}
    for tbl, fname in (("flat_dev_problems", "devprob.parquet"),
                       ("flat_pat_problems", "patprob.parquet")):
        if not table_exists(con, tbl):
            print(f"    (skipped, {tbl} absent)")
            continue
        bridges[tbl] = copy_to_parquet(con, f"""
            SELECT
                TRY_CAST(f.MDR_REPORT_KEY AS UINTEGER)     AS k,
                f.code                                     AS code,
                m.product_code                             AS pc,
                CAST(m.report_year AS USMALLINT)           AS y
            FROM {tbl} f
            JOIN mdr_flat m USING (MDR_REPORT_KEY)
            WHERE TRY_CAST(f.MDR_REPORT_KEY AS UINTEGER) IS NOT NULL
            ORDER BY m.product_code NULLS LAST, m.report_year, f.code, k
        """, os.path.join(out, fname), row_group_size=100_000)
    manifest["files"]["problems"] = bridges
    # Consumers must know the layout changed; the app checks this before
    # using the pushdown query shape.
    manifest["bridge_schema"] = "v2-int-key-pc-year"

    # -------------------------------------------------------- dictionaries
    print("  [3/5] code dictionaries")
    dicts = {}
    for tbl, fname in (("device_problem_dict", "dict_device.parquet"),
                       ("patient_problem_dict", "dict_patient.parquet")):
        if not table_exists(con, tbl):
            print(f"    (skipped, {tbl} absent)")
            continue
        dcols = columns_of(con, tbl)
        if {"FDA_CODE", "TERM"}.issubset(dcols):
            sel = ('SELECT TRIM("FDA_CODE") AS FDA_CODE, TRIM("TERM") AS TERM'
                   + (', "NCIT_CODE", "IMDRF_CODE"' if "NCIT_CODE" in dcols else "")
                   + f' FROM "{tbl}" WHERE "FDA_CODE" IS NOT NULL')
        else:
            # v1-style single mangled column: split it back out.
            merged = next((c for c in dcols if "FDA_CODE" in c and "TERM" in c), None)
            if not merged:
                print(f"    (skipped, {tbl} has an unrecognised shape: {sorted(dcols)})")
                continue
            sel = (f'SELECT TRIM(list_extract(str_split("{merged}", \',\'), 1)) AS FDA_CODE, '
                   f'TRIM(list_extract(str_split("{merged}", \',\'), 2)) AS TERM '
                   f'FROM "{tbl}"')
        dicts[tbl] = copy_to_parquet(con, sel, os.path.join(out, fname),
                                     row_group_size=10_000)
    manifest["files"]["dicts"] = dicts

    # ----------------------------------------------------------- rollups
    print("  [4/5] precomputed rollups")
    rollups = {}
    for tbl in ("agg_yearly_event", "agg_yearly_outcomes", "agg_product_code",
                "agg_manufacturer", "agg_country", "agg_dev_problems_global",
                "agg_pat_problems_global"):
        if table_exists(con, tbl):
            rollups[tbl] = copy_to_parquet(
                con, f"SELECT * FROM {tbl}",
                os.path.join(out, f"{tbl}.parquet"), row_group_size=10_000)
    manifest["files"]["rollups"] = rollups

    # ------------------------------------------------------- optional foi
    if args.include_foi:
        print("  [5/5] untruncated narratives from foi (this is the slow step)")
        if not table_exists(con, "foi"):
            print("    (skipped, foi absent)")
        else:
            foi_files = []
            for label, pred in ([("pre2014", f"m.report_year IS NULL OR m.report_year < {SPLIT_FROM_YEAR}")]
                                + [(str(int(y)), f"m.report_year = {int(y)}") for y in split_years]):
                foi_files.append(copy_to_parquet(con, f"""
                    SELECT f.MDR_REPORT_KEY, f.TEXT_TYPE_CODE,
                           f.PATIENT_SEQUENCE_NUMBER, f.FOI_TEXT
                    FROM foi f
                    JOIN (SELECT MDR_REPORT_KEY, report_year FROM mdr_flat) m
                      USING (MDR_REPORT_KEY)
                    WHERE f.FOI_TEXT IS NOT NULL AND ({pred})
                    ORDER BY f.MDR_REPORT_KEY, f.TEXT_TYPE_CODE
                """, os.path.join(out, f"foi_{label}.parquet")))
            manifest["files"]["foi"] = foi_files
    else:
        print("  [5/5] foi export skipped (--include-foi to enable)")

    # ------------------------------------------------------- summary.json
    print("\n  Building summary.json (corpus figures for instant landing render)")
    total_rows = con.execute("SELECT COUNT(*) FROM mdr_flat").fetchone()[0]
    vintage = con.execute(
        "SELECT MAX(DATE_PREF) FROM mdr_flat WHERE DATE_PREF IS NOT NULL").fetchone()[0]
    by_year = con.execute("""
        SELECT report_year AS year, COUNT(*) AS reports,
               SUM(CASE WHEN EVENT_TYPE='D'  THEN 1 ELSE 0 END) AS deaths,
               SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS injuries,
               SUM(CASE WHEN EVENT_TYPE='M'  THEN 1 ELSE 0 END) AS malfunctions,
               SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS serious
        FROM mdr_flat WHERE report_year IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    headline = con.execute("""
        SELECT COUNT(*) AS reports,
               COUNT(DISTINCT REPORT_NUMBER) AS events,
               COUNT(DISTINCT product_code) AS product_codes,
               COUNT(DISTINCT manufacturer) AS manufacturers,
               SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS outcome_deaths,
               SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS serious,
               -- The disproportionality comparator. Publishing it here spares
               -- the browser a 20.7-million-row COUNT across every year file
               -- on the critical path of the slowest panel (717 ms measured),
               -- for a number that is constant between data releases.
               SUM(CASE WHEN report_year IS NULL OR report_year >= ?
                        THEN 1 ELSE 0 END) AS code_eligible
        FROM mdr_flat
    """, [CODE_ELIGIBLE_FROM_YEAR]).fetchone()

    data_bytes = sum(
        os.path.getsize(os.path.join(out, f))
        for f in os.listdir(out) if f.endswith(".parquet")
    )
    summary = {
        "generated_utc": manifest["generated_utc"],
        "data_vintage": str(vintage) if vintage else None,
        "total_reports": int(total_rows),
        "unique_events": int(headline[1]),
        "product_codes": int(headline[2]),
        "manufacturers": int(headline[3]),
        "outcome_deaths": int(headline[4] or 0),
        "serious_outcomes": int(headline[5] or 0),
        "code_eligible_reports": int(headline[6] or 0),
        "year_min": int(min(years)) if years else None,
        "year_max": int(max(years)) if years else None,
        "by_year": [
            {"year": int(r[0]), "reports": int(r[1]), "deaths": int(r[2]),
             "injuries": int(r[3]), "malfunctions": int(r[4]),
             "serious": int(r[5] or 0)}
            for r in by_year
        ],
        "web_tier_bytes": int(data_bytes),
        "source_db_bytes": int(os.path.getsize(args.db)),
    }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    manifest["total_reports"] = int(total_rows)
    manifest["data_vintage"] = summary["data_vintage"]
    manifest["web_tier_bytes"] = int(data_bytes)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    con.close()

    print()
    print("=" * 72)
    print(f"  Parquet payload : {human(data_bytes)}")
    print(f"  Source database : {human(summary['source_db_bytes'])}")
    if data_bytes:
        print(f"  Reduction       : {summary['source_db_bytes'] / data_bytes:,.0f}x smaller")
    print(f"  Reports         : {total_rows:,}")
    print(f"  Data vintage    : {summary['data_vintage']}")
    print(f"  Written to      : {out}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
