"""
Targeted loader for patient_problem_codes
==========================================

Adds the missing per-report patient problem code data to an existing MAUDE
DuckDB without rebuilding anything else. Use this when:

  - Your validator shows `patient_problem_codes` with 0 rows
  - You downloaded `patientproblemcode.zip` from the FDA after the first ingest
  - You want to avoid a 1-2 hour full re-ingest

What it does
------------
1. Inspects the source file to detect its format. Three known variants:
   - 2-column headerless: MDR_REPORT_KEY | PROBLEM_CODE             (pre-2020)
   - 3-column headerless: MDR_REPORT_KEY | PATIENT_SEQUENCE_NUMBER | PROBLEM_CODE
   - 5-column WITH HEADER: MDR_REPORT_KEY | PATIENT_SEQUENCE_NO | PROBLEM_CODE
                           | DATE_ADDED | DATE_CHANGED              (current FDA format)
2. Loads into `patient_problem_codes` table, replacing any existing version.
3. Creates the MDR_REPORT_KEY index for join performance.
4. If `mdr_flat` already exists, rebuilds the per-MDR `patient_problem_codes`
   aggregate column on it so the dashboard sees patient problems without
   a full analytic rebuild.

Usage
-----
    python maude_load_patient_problems.py --file patientproblemcode.txt \\
                                          --db maude_final.duckdb --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


def info(msg: str) -> None:
    print(msg, flush=True)


def sniff_file(path: Path) -> dict:
    """Returns dict with: ncols, has_header, header_names, first_data_row."""
    with open(path, "rb") as f:
        head = f.read(8192)
    try:
        text = head.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = head.decode("latin-1", errors="replace")
    text = text.lstrip("\ufeff")

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return {"ncols": 0, "has_header": False, "header_names": [],
                "first_data_row": []}

    first = lines[0].strip().split("|")
    # A header has non-numeric first field that looks like a column name
    # (all caps, contains underscores or alphabetic).
    first_field = first[0].strip()
    has_header = (not first_field.isdigit() and
                  bool(first_field) and
                  any(c.isalpha() for c in first_field))

    if has_header:
        header_names = [c.strip() for c in first]
        first_data_row = (lines[1].strip().split("|") if len(lines) > 1 else [])
    else:
        header_names = []
        first_data_row = [c.strip() for c in first]

    return {
        "ncols": len(first),
        "has_header": has_header,
        "header_names": header_names,
        "first_data_row": first_data_row,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Load patient_problem_codes into existing DB")
    ap.add_argument("--file", required=True,
                    help="Path to patientproblemcode.txt")
    ap.add_argument("--db", default="maude_final.duckdb",
                    help="Existing MAUDE DuckDB to update")
    ap.add_argument("--no-rebuild-flat", action="store_true",
                    help="Skip rebuilding the mdr_flat aggregate column")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (for scripted / CI use)")
    args = ap.parse_args()

    src = Path(args.file).resolve()
    if not src.is_file():
        info(f"ERROR: file not found: {src}")
        return 2
    if not Path(args.db).is_file():
        info(f"ERROR: database not found: {args.db}")
        return 2

    info(f"Source file: {src}")
    info(f"File size:   {src.stat().st_size / 1_000_000:.1f} MB")
    info(f"Database:    {args.db}")
    info("")

    # Step 1: Detect format
    info("=> Detecting file format ...")
    s = sniff_file(src)
    ncols = s["ncols"]
    has_header = s["has_header"]
    info(f"   columns:    {ncols}")
    info(f"   has header: {has_header}")
    if has_header:
        info(f"   header:     {s['header_names']}")
        info(f"   first data: {s['first_data_row']}")
    else:
        info(f"   first row:  {s['first_data_row']}")

    # Step 2: Map to a loading strategy
    # We want the loaded table to have at minimum:
    #   MDR_REPORT_KEY, PATIENT_SEQUENCE_NUMBER, PROBLEM_CODE
    # plus DATE_ADDED, DATE_CHANGED if present.

    if ncols == 2 and not has_header:
        format_name = "2-col headerless (pre-2020)"
        load_columns = {"MDR_REPORT_KEY": 0, "PROBLEM_CODE": 1}
        seq_default = "NULL::VARCHAR"
        date_added_default = "NULL::VARCHAR"
        date_changed_default = "NULL::VARCHAR"
    elif ncols == 3 and not has_header:
        format_name = "3-col headerless"
        load_columns = {
            "MDR_REPORT_KEY": 0,
            "PATIENT_SEQUENCE_NUMBER": 1,
            "PROBLEM_CODE": 2,
        }
        seq_default = None
        date_added_default = "NULL::VARCHAR"
        date_changed_default = "NULL::VARCHAR"
    elif ncols == 5 and has_header:
        # Current FDA format:
        # MDR_REPORT_KEY|PATIENT_SEQUENCE_NO|PROBLEM_CODE|DATE_ADDED|DATE_CHANGED
        format_name = "5-col with header (current FDA format)"
        # Map by header name to be robust against future column reordering
        col_map = {h.upper(): i for i, h in enumerate(s["header_names"])}
        load_columns = {}
        for canonical, aliases in [
            ("MDR_REPORT_KEY",  ["MDR_REPORT_KEY"]),
            ("PATIENT_SEQUENCE_NUMBER", ["PATIENT_SEQUENCE_NUMBER", "PATIENT_SEQUENCE_NO", "PAT_SEQ_NO", "SEQUENCE_NO"]),
            ("PROBLEM_CODE",    ["PROBLEM_CODE", "PAT_PROBLEM_CODE", "PATIENT_PROBLEM_CODE"]),
            ("DATE_ADDED",      ["DATE_ADDED"]),
            ("DATE_CHANGED",    ["DATE_CHANGED"]),
        ]:
            for alias in aliases:
                if alias in col_map:
                    load_columns[canonical] = col_map[alias]
                    break
        if "MDR_REPORT_KEY" not in load_columns or "PROBLEM_CODE" not in load_columns:
            info(f"ERROR: could not find MDR_REPORT_KEY or PROBLEM_CODE in header: {s['header_names']}")
            return 2
        seq_default = None
        date_added_default = "NULL::VARCHAR"
        date_changed_default = "NULL::VARCHAR"
    else:
        info(f"ERROR: unrecognised format: ncols={ncols}, has_header={has_header}")
        info("Known formats are 2-col headerless, 3-col headerless, or 5-col with header.")
        info(f"Got header: {s['header_names']}")
        info(f"Got first row: {s['first_data_row']}")
        return 2

    info(f"   -> Format: {format_name}")
    info(f"   -> Column mapping: {load_columns}")
    info("")

    # Sanity check the data row before loading the whole file
    if s["first_data_row"]:
        first_mdr = s["first_data_row"][load_columns.get("MDR_REPORT_KEY", 0)]
        first_code = s["first_data_row"][load_columns.get("PROBLEM_CODE", 1)]
        info(f"   Sample data row: MDR_REPORT_KEY={first_mdr!r}, PROBLEM_CODE={first_code!r}")
        if not first_mdr.isdigit() or len(first_mdr) < 4:
            info(f"WARNING: first MDR_REPORT_KEY {first_mdr!r} doesn't look like a valid key.")
            if not args.yes:
                ans = input("Continue anyway? [y/N]: ").strip().lower()
                if ans != "y":
                    info("Aborted.")
                    return 1
    info("")

    # Step 3: Load
    con = duckdb.connect(args.db)
    try:
        con.execute("PRAGMA memory_limit = '12GB';")
        con.execute("PRAGMA preserve_insertion_order = false;")
        con.execute("PRAGMA threads = 4;")
        con.execute("PRAGMA temp_directory = '.';")
    except Exception:
        pass

    info("=> Loading patient_problem_codes ...")
    con.execute("DROP TABLE IF EXISTS patient_problem_codes;")

    src_str = str(src).replace("\\", "/")  # forward slashes for DuckDB SQL

    # Build the read_csv columns spec. We always read all source columns as
    # VARCHAR (avoids parsing issues with the date columns) and select the
    # ones we need into our canonical schema.
    src_col_names = [f"col{i}" for i in range(ncols)]
    src_col_spec = ", ".join(f"'{c}': 'VARCHAR'" for c in src_col_names)

    # Build the SELECT projection. For each canonical column, either pull the
    # mapped source column or substitute NULL.
    def col_or_null(canonical: str, sql_default: str = "NULL::VARCHAR") -> str:
        if canonical in load_columns:
            return f"col{load_columns[canonical]} AS {canonical}"
        return f"{sql_default} AS {canonical}"

    select_clause = ", ".join([
        col_or_null("MDR_REPORT_KEY"),
        col_or_null("PATIENT_SEQUENCE_NUMBER", "NULL::VARCHAR"),
        col_or_null("PROBLEM_CODE"),
        col_or_null("DATE_ADDED", "NULL::VARCHAR"),
        col_or_null("DATE_CHANGED", "NULL::VARCHAR"),
    ])

    header_flag = "true" if has_header else "false"

    load_sql = f"""
        CREATE TABLE patient_problem_codes AS
        SELECT {select_clause}
        FROM read_csv(
            '{src_str}',
            delim='|',
            header={header_flag},
            columns={{{src_col_spec}}},
            quote='',
            escape='',
            strict_mode=false,
            ignore_errors=true,
            max_line_size=4000000
        )
        WHERE col{load_columns["MDR_REPORT_KEY"]} IS NOT NULL
          AND TRIM(col{load_columns["MDR_REPORT_KEY"]}) <> ''
          AND col{load_columns["PROBLEM_CODE"]} IS NOT NULL
          AND TRIM(col{load_columns["PROBLEM_CODE"]}) <> '';
    """

    con.execute(load_sql)

    n = con.execute("SELECT COUNT(*) FROM patient_problem_codes").fetchone()[0]
    info(f"   = loaded {n:,} rows")

    if n < 1_000_000:
        info(f"WARNING: only {n:,} rows loaded; expected ~20M for a current MAUDE corpus.")

    # Step 4: Index
    info("=> Creating index ...")
    try:
        con.execute("CREATE INDEX IF NOT EXISTS idx_ppc_key ON patient_problem_codes(MDR_REPORT_KEY);")
        info("   = idx_ppc_key created")
    except Exception as e:
        info(f"   ! index failed: {e}")

    # Step 5: Rebuild mdr_flat's patient_problem_codes column
    mdr_flat_exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name='mdr_flat'"
    ).fetchone()[0] == 1

    if mdr_flat_exists and not args.no_rebuild_flat:
        info("")
        info("=> Rebuilding patient_problem_codes column on mdr_flat ...")

        info("   -> Aggregating codes per MDR ...")
        con.execute("DROP TABLE IF EXISTS _x_pat_prob_agg;")
        con.execute("""
            CREATE TABLE _x_pat_prob_agg AS
            SELECT MDR_REPORT_KEY,
                   string_agg(DISTINCT CAST(PROBLEM_CODE AS VARCHAR), ', '
                              ORDER BY CAST(PROBLEM_CODE AS VARCHAR)) AS codes
            FROM patient_problem_codes
            WHERE PROBLEM_CODE IS NOT NULL AND TRIM(PROBLEM_CODE) <> ''
            GROUP BY 1;
        """)
        n_agg = con.execute("SELECT COUNT(*) FROM _x_pat_prob_agg").fetchone()[0]
        info(f"      = {n_agg:,} MDRs with patient problem codes")

        try:
            con.execute("CREATE INDEX idx_xppa_key ON _x_pat_prob_agg(MDR_REPORT_KEY);")
        except Exception:
            pass

        info("   -> Updating mdr_flat.patient_problem_codes ...")
        con.execute("""
            UPDATE mdr_flat
            SET patient_problem_codes = a.codes
            FROM _x_pat_prob_agg a
            WHERE mdr_flat.MDR_REPORT_KEY = a.MDR_REPORT_KEY;
        """)
        n_updated = con.execute(
            "SELECT COUNT(*) FROM mdr_flat WHERE patient_problem_codes IS NOT NULL"
        ).fetchone()[0]
        info(f"      = {n_updated:,} mdr_flat rows now have patient_problem_codes populated")

        info("   -> Rebuilding flat_pat_problems and agg_pat_problems_global ...")
        con.execute("""
            CREATE OR REPLACE TABLE flat_pat_problems AS
            SELECT MDR_REPORT_KEY, TRIM(c.value) AS code
            FROM mdr_flat,
                 unnest(string_split(COALESCE(patient_problem_codes, ''), ',')) c(value)
            WHERE TRIM(c.value) <> '';
        """)
        con.execute("""
            CREATE OR REPLACE TABLE agg_pat_problems_global AS
            SELECT code, COUNT(DISTINCT MDR_REPORT_KEY)::BIGINT AS n
            FROM flat_pat_problems GROUP BY 1;
        """)
        try:
            con.execute("CREATE INDEX IF NOT EXISTS idx_fpp_key  ON flat_pat_problems(MDR_REPORT_KEY);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_fpp_code ON flat_pat_problems(code);")
        except Exception:
            pass

        n_fp = con.execute("SELECT COUNT(*) FROM flat_pat_problems").fetchone()[0]
        n_ap = con.execute("SELECT COUNT(*) FROM agg_pat_problems_global").fetchone()[0]
        info(f"      = flat_pat_problems:       {n_fp:>12,}")
        info(f"      = agg_pat_problems_global: {n_ap:>12,}")

        con.execute("DROP TABLE IF EXISTS _x_pat_prob_agg;")
    elif args.no_rebuild_flat:
        info("")
        info("=> Skipped rebuilding mdr_flat (--no-rebuild-flat).")
    else:
        info("")
        info("=> mdr_flat not present yet; will be picked up on next analytic build.")

    con.close()
    info("")
    info("Done.")
    info("")
    info("Run the validator to confirm:")
    info(f"  python maude_validate_db.py --db {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
