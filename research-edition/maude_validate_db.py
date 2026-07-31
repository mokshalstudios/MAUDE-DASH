"""
Database validator for MAUDE-Dash
==================================

Runs a series of sanity checks on a built MAUDE database and reports anything
suspicious. Exit code 0 = clean, 1 = warnings, 2 = errors.

Run after every ingest + analytic build:
    python maude_validate_db.py --db maude_final.duckdb
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import duckdb


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"[ERROR] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[WARN]  {msg}")

    def ok(self, msg: str) -> None:
        self.info.append(msg)
        print(f"[OK]    {msg}")

    def section(self, name: str) -> None:
        print(f"\n--- {name} ---")

    def exit_code(self) -> int:
        if self.errors:
            return 2
        if self.warnings:
            return 1
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]
    ).fetchone()[0] == 1


def col_exists(con: duckdb.DuckDBPyConnection, table: str, col: str) -> bool:
    try:
        return col in {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
    except Exception:
        return False


def count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_required_tables(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Required tables")
    required = ["mdr", "device"]
    optional = ["patient", "foi", "foidevproblem", "patient_problem_codes",
                "device_problem_dict", "patient_problem_dict"]
    for t in required:
        if table_exists(con, t):
            n = count(con, t)
            r.ok(f"{t} present ({n:,} rows)")
            if n == 0:
                r.err(f"{t} is empty — ingest didn't populate it")
        else:
            r.err(f"REQUIRED table {t} missing")
    for t in optional:
        if table_exists(con, t):
            r.ok(f"{t} present ({count(con, t):,} rows)")
        else:
            r.warn(f"Optional table {t} missing — some dashboard tabs will be limited")


def check_referential_integrity(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    """How many MDRs in dependent tables have no matching mdr row?"""
    r.section("Referential integrity")
    base_keys = "(SELECT DISTINCT MDR_REPORT_KEY FROM mdr)"
    for child in ["device", "patient", "foi", "foidevproblem", "patient_problem_codes"]:
        if not table_exists(con, child):
            continue
        try:
            n_orphan = con.execute(
                f"SELECT COUNT(*) FROM {child} c "
                f"WHERE NOT EXISTS (SELECT 1 FROM mdr m WHERE m.MDR_REPORT_KEY = c.MDR_REPORT_KEY)"
            ).fetchone()[0]
            if n_orphan == 0:
                r.ok(f"{child}: no orphan rows")
            else:
                pct = 100.0 * n_orphan / max(1, count(con, child))
                r.warn(
                    f"{child}: {n_orphan:,} rows ({pct:.1f}%) reference an MDR_REPORT_KEY "
                    "not in mdr. This usually means partial year coverage."
                )
        except Exception as e:
            r.warn(f"{child}: integrity check failed ({e})")


def check_dates(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Date typing")
    if col_exists(con, "mdr", "DATE_PREF"):
        # Coverage
        n_total = count(con, "mdr")
        n_dated = con.execute(
            "SELECT COUNT(*) FROM mdr WHERE DATE_PREF IS NOT NULL"
        ).fetchone()[0]
        pct = 100.0 * n_dated / n_total if n_total else 0
        if pct >= 95:
            r.ok(f"DATE_PREF populated for {pct:.1f}% of mdr rows")
        elif pct >= 70:
            r.warn(f"DATE_PREF populated for only {pct:.1f}% — some dates failed to parse")
        else:
            r.err(f"DATE_PREF populated for only {pct:.1f}% — likely a parsing problem")
        # Range sanity
        rng = con.execute(
            "SELECT MIN(DATE_PREF), MAX(DATE_PREF) FROM mdr WHERE DATE_PREF IS NOT NULL"
        ).fetchone()
        if rng[0] and rng[0].year < 1990:
            r.warn(f"Earliest DATE_PREF is {rng[0]} — suspiciously old")
        if rng[1] and rng[1].year > 2030:
            r.warn(f"Latest DATE_PREF is {rng[1]} — suspiciously far in future")
        if rng[0] and rng[1]:
            r.ok(f"DATE_PREF range: {rng[0]} to {rng[1]}")
    else:
        r.err("mdr.DATE_PREF missing — run maude_ingest_v2 to type the dates")


def check_narratives(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Narratives")
    if not table_exists(con, "foi"):
        return
    # Look for signs of multi-line reassembly failure: a tell-tale sign would
    # be many rows with empty MDR_REPORT_KEY (orphan continuation lines).
    n_total = count(con, "foi")
    n_orphan = con.execute(
        "SELECT COUNT(*) FROM foi WHERE MDR_REPORT_KEY IS NULL OR TRIM(MDR_REPORT_KEY) = ''"
    ).fetchone()[0]
    if n_orphan == 0:
        r.ok(f"foi: no orphan rows ({n_total:,} narrative records)")
    else:
        pct = 100.0 * n_orphan / n_total
        if pct > 0.5:
            r.warn(
                f"foi has {n_orphan:,} rows ({pct:.1f}%) without an MDR_REPORT_KEY. "
                "Possible incomplete multi-line reassembly."
            )
        else:
            r.ok(f"foi: {n_orphan} ({pct:.2f}%) orphan rows — acceptable")
    # Empty-text rate
    if col_exists(con, "foi", "FOI_TEXT"):
        n_empty = con.execute(
            "SELECT COUNT(*) FROM foi WHERE FOI_TEXT IS NULL OR TRIM(FOI_TEXT) = ''"
        ).fetchone()[0]
        pct = 100.0 * n_empty / n_total if n_total else 0
        if pct > 5:
            r.warn(f"{pct:.1f}% of foi rows have empty FOI_TEXT — possible parse issue")


def check_dictionaries(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Dictionaries")
    for tbl in ["device_problem_dict", "patient_problem_dict"]:
        if not table_exists(con, tbl):
            r.warn(f"{tbl} missing — problem codes will show as numbers, not names")
            continue
        cols = {c[0] for c in con.execute(f'DESCRIBE "{tbl}"').fetchall()}
        n = count(con, tbl)
        if {"FDA_CODE", "TERM"}.issubset(cols):
            r.ok(f"{tbl}: {n} entries, proper columns")
        else:
            # The v1 ingest bug: dictionary loaded as single mangled column
            merged = next((c for c in cols if "FDA_CODE" in c and "TERM" in c), None)
            if merged:
                r.warn(
                    f"{tbl}: only one column ({merged!r}) — looks like the CSV "
                    "was ingested as pipe-delimited. The dashboard handles this "
                    "but rebuilding with maude_ingest_v2 would be cleaner."
                )
            else:
                r.err(f"{tbl}: missing required columns FDA_CODE / TERM")


def check_clinical_fields(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Clinical fields (v2 analytic build)")
    if not table_exists(con, "mdr_flat"):
        r.warn("mdr_flat missing — run maude_analytic_build_v2 to enable v3/v4 dashboards")
        return
    required = [
        "any_serious_outcome", "outcome_death", "outcome_codes_raw",
        "device_age_days", "implant_flag", "is_supplement", "initial_report",
        "reporter_country_code",
    ]
    cols = {c[0] for c in con.execute('DESCRIBE mdr_flat').fetchall()}
    missing = [c for c in required if c not in cols]
    if missing:
        r.warn(f"mdr_flat lacks v2 clinical fields: {missing}. Re-run "
              "maude_analytic_build_v2 to enable Clinical Outcomes etc.")
    else:
        r.ok("mdr_flat has all v2 clinical fields")
        # Sanity check the flags
        n_serious = con.execute(
            "SELECT COUNT(*) FROM mdr_flat WHERE any_serious_outcome = TRUE"
        ).fetchone()[0]
        n_with_outcome = con.execute(
            "SELECT COUNT(*) FROM mdr_flat WHERE outcome_codes_raw IS NOT NULL "
            "AND TRIM(outcome_codes_raw) <> ''"
        ).fetchone()[0]
        r.ok(f"  Reports with outcome data: {n_with_outcome:,}")
        r.ok(f"  Reports flagged 'any serious': {n_serious:,}")
        if n_with_outcome == 0:
            r.warn(
                "Outcome data is uniformly empty. Patient outcomes are critical "
                "for clinical analyses; the patient table may not have "
                "SEQUENCE_NUMBER_OUTCOME populated."
            )


def check_event_type_distribution(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("EVENT_TYPE distribution")
    df = con.execute(
        "SELECT EVENT_TYPE, COUNT(*) AS n FROM mdr GROUP BY 1 ORDER BY n DESC"
    ).fetchdf()
    total = df["n"].sum()
    seen = set()
    for _, row in df.iterrows():
        et = row["EVENT_TYPE"]
        pct = 100.0 * row["n"] / total
        seen.add(et)
        r.ok(f"  {et!r}: {row['n']:,} ({pct:.1f}%)")
    expected = {"D", "IN", "M"}
    missing = expected - seen
    if missing:
        r.warn(f"No reports with EVENT_TYPE in {missing}. Date range might exclude them.")


def check_year_coverage(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Year coverage")
    if not table_exists(con, "mdr_flat"):
        return
    df = con.execute(
        "SELECT report_year, COUNT(*) AS n FROM mdr_flat "
        "WHERE report_year IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchdf()
    if df.empty:
        r.warn("No year data in mdr_flat")
        return
    years = df["report_year"].tolist()
    for y in years:
        r.ok(f"  {int(y)}: {int(df[df['report_year']==y]['n'].iloc[0]):,}")
    # Detect implausibly small years
    median_n = df["n"].median()
    suspicious = df[df["n"] < median_n * 0.1]
    if len(suspicious) > 0 and len(df) > 2:
        sus_years = ", ".join(str(int(y)) for y in suspicious["report_year"])
        r.warn(
            f"Year(s) with <10% of median volume: {sus_years}. "
            "Likely a partial year — exclude from rate comparisons."
        )


def check_indexes(con: duckdb.DuckDBPyConnection, r: Report) -> None:
    r.section("Indexes")
    try:
        idxs = con.execute(
            "SELECT index_name, table_name FROM duckdb_indexes() ORDER BY 2, 1"
        ).fetchall()
        if idxs:
            for name, tbl in idxs:
                r.ok(f"  {tbl}.{name}")
        else:
            r.warn("No indexes found — queries may be slow on large databases")
    except Exception:
        r.ok("(index introspection unavailable in this DuckDB version)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Validate MAUDE DuckDB build")
    ap.add_argument("--db", default="maude_final.duckdb")
    args = ap.parse_args(argv)

    print(f"Validating: {args.db}")
    try:
        con = duckdb.connect(args.db, read_only=True)
    except Exception as e:
        print(f"ERROR: cannot open {args.db}: {e}")
        return 2

    r = Report()
    check_required_tables(con, r)
    check_referential_integrity(con, r)
    check_dates(con, r)
    check_narratives(con, r)
    check_dictionaries(con, r)
    check_event_type_distribution(con, r)
    check_clinical_fields(con, r)
    check_year_coverage(con, r)
    check_indexes(con, r)

    print(f"\n{'='*60}")
    print(f"Summary: {len(r.info)} OK, {len(r.warnings)} warnings, {len(r.errors)} errors")
    code = r.exit_code()
    if code == 0:
        print("Database looks good.")
    elif code == 1:
        print("Database is usable but has warnings — review above.")
    else:
        print("Database has errors — fix before relying on results.")
    return code


if __name__ == "__main__":
    sys.exit(main())
