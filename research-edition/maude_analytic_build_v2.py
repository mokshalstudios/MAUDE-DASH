"""
MAUDE analytic-table builder v2 (memory-safe)
==============================================

Builds the denormalised `mdr_flat` table plus rollup tables for the v4
dashboard. Memory-friendly version that works on a 16-32 GB workstation
against a 20M-row MDR corpus.

Strategy:
  1. Persist each per-MDR aggregate as a real table (not a CTE) - this
     forces DuckDB to materialise small inputs to disk before joining.
  2. Compose `mdr_flat` year-by-year via INSERT, bounding peak memory to
     ~2-3M rows per chunk.
  3. Drop intermediate tables as soon as they're no longer needed.

Schema is byte-identical to the prior v2 build, so dashboard v4 needs no
changes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import duckdb


def info(msg: str) -> None:
    print(msg, flush=True)


def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return (
        con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?", [name],
        ).fetchone()[0] == 1
    )


def has_col(con: duckdb.DuckDBPyConnection, table: str, col: str) -> bool:
    try:
        return col in {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DEVICE_AGE_TEXT parser
# ---------------------------------------------------------------------------

DEVICE_AGE_SQL = r"""
CASE
  WHEN DEVICE_AGE_TEXT IS NULL OR TRIM(DEVICE_AGE_TEXT) = '' THEN NULL
  WHEN upper(regexp_extract(DEVICE_AGE_TEXT, '([A-Za-z]+)', 1)) = 'YR'
    THEN try_cast(regexp_extract(DEVICE_AGE_TEXT, '([0-9]+\.?[0-9]*)', 1) AS DOUBLE) * 365.25
  WHEN upper(regexp_extract(DEVICE_AGE_TEXT, '([A-Za-z]+)', 1)) = 'MO'
    THEN try_cast(regexp_extract(DEVICE_AGE_TEXT, '([0-9]+\.?[0-9]*)', 1) AS DOUBLE) * 30.4375
  WHEN upper(regexp_extract(DEVICE_AGE_TEXT, '([A-Za-z]+)', 1)) = 'WK'
    THEN try_cast(regexp_extract(DEVICE_AGE_TEXT, '([0-9]+\.?[0-9]*)', 1) AS DOUBLE) * 7.0
  WHEN upper(regexp_extract(DEVICE_AGE_TEXT, '([A-Za-z]+)', 1)) = 'DY'
    THEN try_cast(regexp_extract(DEVICE_AGE_TEXT, '([0-9]+\.?[0-9]*)', 1) AS DOUBLE)
  WHEN upper(regexp_extract(DEVICE_AGE_TEXT, '([A-Za-z]+)', 1)) = 'HR'
    THEN try_cast(regexp_extract(DEVICE_AGE_TEXT, '([0-9]+\.?[0-9]*)', 1) AS DOUBLE) / 24.0
  ELSE NULL
END
"""

OUTCOME_LABELS = {
    "D": "death", "L": "life_threatening", "H": "hospitalization",
    "S": "disability", "C": "congenital_anomaly",
    "R": "required_intervention", "O": "other",
}


def configure_for_memory(con: duckdb.DuckDBPyConnection) -> None:
    """Apply DuckDB pragmas to reduce peak memory usage during the build.

    On a 24-32 GB Windows workstation, DuckDB's auto-detect picks
    ~80% of physical RAM, which is too high once Windows reserves
    its share. We cap at 14 GB and force temp-file spilling.
    """
    pragmas = [
        "PRAGMA preserve_insertion_order = false",
        "PRAGMA threads = 4",
        "PRAGMA memory_limit = '14GB'",
        "PRAGMA temp_directory = '.'",
    ]
    for p in pragmas:
        try:
            con.execute(p)
        except Exception as e:
            info(f"   ! Could not set {p}: {e}")


def build_intermediates(con: duckdb.DuckDBPyConnection) -> dict:
    """Build per-MDR aggregate tables. Returns feature-detection dict."""

    feat = {
        "have_patient": table_exists(con, "patient"),
        "have_ppc": table_exists(con, "patient_problem_codes"),
        "have_fdp": table_exists(con, "foidevproblem"),
        "have_foi": table_exists(con, "foi"),
        "have_text_type": table_exists(con, "foi") and has_col(con, "foi", "TEXT_TYPE_CODE"),
        "have_age_years": table_exists(con, "patient") and has_col(con, "patient", "AGE_YEARS"),
        "have_outcome": table_exists(con, "patient") and has_col(con, "patient", "SEQUENCE_NUMBER_OUTCOME"),
        "have_date_pref": has_col(con, "mdr", "DATE_PREF"),
        "have_date_event": has_col(con, "mdr", "DATE_OF_EVENT_D"),
        "have_rwd": has_col(con, "mdr", "IS_RWD_SOURCED"),
        "have_fwd": has_col(con, "mdr", "IS_FORWARDED_803_22_B2"),
        "have_redact_b4": has_col(con, "mdr", "HAS_REDACTION_B4"),
        "have_redact_b6": has_col(con, "mdr", "HAS_REDACTION_B6"),
        "have_country": has_col(con, "mdr", "REPORTER_COUNTRY_CODE"),
        "have_supplement": has_col(con, "mdr", "SUPPLEMENT_NUMBER"),
        "have_adverse_flag": has_col(con, "mdr", "ADVERSE_EVENT_FLAG"),
        "have_problem_flag": has_col(con, "mdr", "PRODUCT_PROBLEM_FLAG"),
        "have_device_age": has_col(con, "device", "DEVICE_AGE_TEXT"),
        "have_implant": has_col(con, "device", "IMPLANT_FLAG"),
        "have_dev_operator": has_col(con, "device", "DEVICE_OPERATOR"),
        "have_dev_eval": has_col(con, "device", "DEVICE_EVALUATED_BY_MANUFACTURER"),
        "have_reproc": has_col(con, "device", "REPROCESSED_AND_REUSED_FLAG"),
    }

    if not feat["have_date_pref"]:
        info("   ! mdr.DATE_PREF missing - run maude_ingest_v2 first.")
        return feat

    info("   -> [1/6] _x_device_first (row_number over device)")
    t0 = time.time()
    if table_exists(con, "_x_device_first"):
        n = con.execute("SELECT COUNT(*) FROM _x_device_first").fetchone()[0]
        info(f"      = already built, {n:,} rows. Skipping.")
    else:
        con.execute("DROP TABLE IF EXISTS _x_device_first;")
        con.execute("""
            CREATE TABLE _x_device_first AS
            SELECT * FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY MDR_REPORT_KEY ORDER BY DEVICE_EVENT_KEY
                ) AS _rn FROM device
            ) WHERE _rn = 1;
        """)
        try:
            con.execute("ALTER TABLE _x_device_first DROP COLUMN _rn;")
        except Exception:
            pass
        try:
            con.execute("CREATE INDEX idx_xdf_key ON _x_device_first(MDR_REPORT_KEY);")
        except Exception:
            pass
        n = con.execute("SELECT COUNT(*) FROM _x_device_first").fetchone()[0]
        info(f"      = {n:,} rows ({(time.time()-t0):.1f}s)")

    info("   -> [2/6] _x_device_count")
    t0 = time.time()
    if table_exists(con, "_x_device_count"):
        n = con.execute("SELECT COUNT(*) FROM _x_device_count").fetchone()[0]
        info(f"      = already built, {n:,} rows. Skipping.")
    else:
        con.execute("DROP TABLE IF EXISTS _x_device_count;")
        con.execute("""
            CREATE TABLE _x_device_count AS
            SELECT MDR_REPORT_KEY, COUNT(*)::INTEGER AS device_count
            FROM device GROUP BY 1;
        """)
        try:
            con.execute("CREATE INDEX idx_xdc_key ON _x_device_count(MDR_REPORT_KEY);")
        except Exception:
            pass
        n = con.execute("SELECT COUNT(*) FROM _x_device_count").fetchone()[0]
        info(f"      = {n:,} rows ({(time.time()-t0):.1f}s)")

    if feat["have_patient"]:
        info("   -> [3/6] _x_patient_agg with outcomes")
        t0 = time.time()
        if table_exists(con, "_x_patient_agg"):
            n = con.execute("SELECT COUNT(*) FROM _x_patient_agg").fetchone()[0]
            info(f"      = already built, {n:,} rows. Skipping.")
        else:
            if feat["have_age_years"]:
                age_aggs = ("MIN(AGE_YEARS) AS age_years_min, "
                           "MAX(AGE_YEARS) AS age_years_max, "
                           "AVG(AGE_YEARS) AS age_years_avg, ")
            else:
                age_aggs = ("NULL::DOUBLE AS age_years_min, "
                           "NULL::DOUBLE AS age_years_max, "
                           "NULL::DOUBLE AS age_years_avg, ")
            if feat["have_outcome"]:
                outcome_aggs = "string_agg(DISTINCT SEQUENCE_NUMBER_OUTCOME, ',') AS outcome_codes_raw, "
            else:
                outcome_aggs = "NULL::VARCHAR AS outcome_codes_raw, "
            con.execute("DROP TABLE IF EXISTS _x_patient_agg;")
            con.execute(f"""
                CREATE TABLE _x_patient_agg AS
                SELECT MDR_REPORT_KEY,
                       COUNT(*)::INTEGER AS patient_count,
                       {age_aggs}
                       {outcome_aggs}
                       string_agg(DISTINCT NULLIF(TRIM(PATIENT_SEX), ''), ',') AS sex_list
                FROM patient GROUP BY 1;
            """)
            try:
                con.execute("CREATE INDEX idx_xpa_key ON _x_patient_agg(MDR_REPORT_KEY);")
            except Exception:
                pass
            n = con.execute("SELECT COUNT(*) FROM _x_patient_agg").fetchone()[0]
            info(f"      = {n:,} rows ({(time.time()-t0):.1f}s)")

    if feat["have_fdp"]:
        info("   -> [4/6] _x_dev_prob_agg")
        t0 = time.time()
        if table_exists(con, "_x_dev_prob_agg"):
            n = con.execute("SELECT COUNT(*) FROM _x_dev_prob_agg").fetchone()[0]
            info(f"      = already built, {n:,} rows. Skipping.")
        else:
            con.execute("DROP TABLE IF EXISTS _x_dev_prob_agg;")
            con.execute("""
                CREATE TABLE _x_dev_prob_agg AS
                SELECT MDR_REPORT_KEY,
                       string_agg(DISTINCT CAST(DEVICE_PROBLEM_CODE AS VARCHAR), ', '
                                  ORDER BY CAST(DEVICE_PROBLEM_CODE AS VARCHAR)) AS codes
                FROM foidevproblem GROUP BY 1;
            """)
            try:
                con.execute("CREATE INDEX idx_xdpa_key ON _x_dev_prob_agg(MDR_REPORT_KEY);")
            except Exception:
                pass
            n = con.execute("SELECT COUNT(*) FROM _x_dev_prob_agg").fetchone()[0]
            info(f"      = {n:,} rows ({(time.time()-t0):.1f}s)")

    if feat["have_ppc"]:
        info("   -> [5/6] _x_pat_prob_agg")
        t0 = time.time()
        if table_exists(con, "_x_pat_prob_agg"):
            n = con.execute("SELECT COUNT(*) FROM _x_pat_prob_agg").fetchone()[0]
            info(f"      = already built, {n:,} rows. Skipping.")
        else:
            n_ppc = con.execute("SELECT COUNT(*) FROM patient_problem_codes").fetchone()[0]
            if n_ppc == 0:
                info("      ! patient_problem_codes has 0 rows - skipping agg")
                feat["have_ppc"] = False
            else:
                con.execute("DROP TABLE IF EXISTS _x_pat_prob_agg;")
                con.execute("""
                    CREATE TABLE _x_pat_prob_agg AS
                    SELECT MDR_REPORT_KEY,
                           string_agg(DISTINCT CAST(PROBLEM_CODE AS VARCHAR), ', '
                                      ORDER BY CAST(PROBLEM_CODE AS VARCHAR)) AS codes
                    FROM patient_problem_codes GROUP BY 1;
                """)
                try:
                    con.execute("CREATE INDEX idx_xppa_key ON _x_pat_prob_agg(MDR_REPORT_KEY);")
                except Exception:
                    pass
                n = con.execute("SELECT COUNT(*) FROM _x_pat_prob_agg").fetchone()[0]
                info(f"      = {n:,} rows ({(time.time()-t0):.1f}s)")

    if feat["have_foi"]:
        info("   -> [6/6] _x_narrative_agg (4KB cap per narrative, chunked)")
        t0 = time.time()
        if feat["have_text_type"]:
            desc_filter = "FILTER (WHERE TEXT_TYPE_CODE = 'D')"
            mfg_filter = "FILTER (WHERE TEXT_TYPE_CODE IN ('N', 'M'))"
        else:
            desc_filter = ""
            mfg_filter = "FILTER (WHERE FALSE)"

        # The narrative aggregation is the highest-memory step in the whole
        # pipeline because each MDR's narrative parts (sometimes hundreds
        # of rows) get concatenated into one VARCHAR. On a 40M-row foi
        # table this can need 30+ GB. We chunk by hashing MDR_REPORT_KEY
        # into N buckets and processing one bucket at a time.

        N_BUCKETS = int(os.environ.get("MAUDE_NARRATIVE_BUCKETS", "16"))
        con.execute("DROP TABLE IF EXISTS _x_narrative_agg;")

        # Create the destination table with the right schema by inserting
        # a sentinel row first, then deleting it. (DuckDB has no
        # CREATE TABLE LIKE for arbitrary SELECT shapes.)
        con.execute(f"""
            CREATE TABLE _x_narrative_agg AS
            SELECT MDR_REPORT_KEY,
                   COUNT(*)::INTEGER AS narr_part_count,
                   substr(string_agg(FOI_TEXT, ' || ') {desc_filter}, 1, 4000) AS narrative_desc,
                   substr(string_agg(FOI_TEXT, ' || ') {mfg_filter}, 1, 4000) AS narrative_mfg
            FROM foi
            WHERE FALSE
            GROUP BY 1;
        """)

        # Iterate buckets. Hash function: hash(MDR_REPORT_KEY) % N_BUCKETS.
        # DuckDB's `hash()` is deterministic and fast.
        for bucket in range(N_BUCKETS):
            tb = time.time()
            try:
                con.execute(f"""
                    INSERT INTO _x_narrative_agg
                    SELECT MDR_REPORT_KEY,
                           COUNT(*)::INTEGER AS narr_part_count,
                           substr(string_agg(FOI_TEXT, ' || ') {desc_filter}, 1, 4000) AS narrative_desc,
                           substr(string_agg(FOI_TEXT, ' || ') {mfg_filter}, 1, 4000) AS narrative_mfg
                    FROM foi
                    WHERE (hash(MDR_REPORT_KEY) % {N_BUCKETS}) = {bucket}
                    GROUP BY 1;
                """)
                running = con.execute("SELECT COUNT(*) FROM _x_narrative_agg").fetchone()[0]
                info(f"      = bucket {bucket+1:>2}/{N_BUCKETS}: cumulative {running:>12,} rows ({(time.time()-tb):.1f}s)")
            except duckdb.OutOfMemoryException as e:
                info(f"      ! bucket {bucket+1}/{N_BUCKETS} OOM: {e}")
                info(f"      ! Try N_BUCKETS = 32 or 64 (edit this script and re-run).")
                raise

        try:
            con.execute("CREATE INDEX idx_xna_key ON _x_narrative_agg(MDR_REPORT_KEY);")
        except Exception:
            pass
        n = con.execute("SELECT COUNT(*) FROM _x_narrative_agg").fetchone()[0]
        info(f"      = total: {n:,} rows ({(time.time()-t0):.1f}s)")

    return feat


def build_select_and_joins(feat: dict) -> tuple[list[str], list[str]]:
    select_cols: list[str] = [
        "m.MDR_REPORT_KEY",
        "m.REPORT_NUMBER",
        "m.EVENT_TYPE",
        "m.DATE_RECEIVED_D",
        ("m.DATE_OF_EVENT_D" if feat["have_date_event"] else "NULL::DATE AS DATE_OF_EVENT_D"),
        "m.DATE_PREF",
        "CAST(strftime(m.DATE_PREF, '%Y') AS INTEGER) AS report_year",
        "CAST(strftime(m.DATE_PREF, '%Y-%m') AS VARCHAR) AS report_month",
    ]
    if feat["have_date_event"]:
        select_cols.append(
            "CASE WHEN m.DATE_OF_EVENT_D IS NOT NULL AND m.DATE_PREF IS NOT NULL "
            "  AND date_diff('day', m.DATE_OF_EVENT_D, m.DATE_PREF) BETWEEN 0 AND 365 "
            "  THEN date_diff('day', m.DATE_OF_EVENT_D, m.DATE_PREF) "
            "  ELSE NULL END AS lag_days"
        )
    else:
        select_cols.append("NULL::INTEGER AS lag_days")

    select_cols += [
        "m.SOURCE_TYPE",
        "m.REPORTER_OCCUPATION_CODE",
        ("m.IS_RWD_SOURCED" if feat["have_rwd"] else "FALSE AS IS_RWD_SOURCED"),
        ("m.IS_FORWARDED_803_22_B2" if feat["have_fwd"] else "FALSE AS IS_FORWARDED_803_22_B2"),
        ("m.HAS_REDACTION_B4" if feat["have_redact_b4"] else "FALSE AS HAS_REDACTION_B4"),
        ("m.HAS_REDACTION_B6" if feat["have_redact_b6"] else "FALSE AS HAS_REDACTION_B6"),
    ]

    if feat["have_country"]:
        select_cols.append("UPPER(TRIM(m.REPORTER_COUNTRY_CODE)) AS reporter_country_code")
    else:
        select_cols.append("NULL::VARCHAR AS reporter_country_code")

    if feat["have_supplement"]:
        select_cols += [
            "(m.SUPPLEMENT_NUMBER IS NOT NULL AND TRIM(m.SUPPLEMENT_NUMBER) <> ''"
            " AND TRIM(m.SUPPLEMENT_NUMBER) <> '0') AS is_supplement",
            "try_cast(NULLIF(TRIM(m.SUPPLEMENT_NUMBER), '') AS INTEGER) AS supplement_number",
            "(m.SUPPLEMENT_NUMBER IS NULL OR TRIM(m.SUPPLEMENT_NUMBER) = ''"
            " OR TRIM(m.SUPPLEMENT_NUMBER) = '0') AS initial_report",
        ]
    else:
        select_cols += [
            "FALSE AS is_supplement",
            "NULL::INTEGER AS supplement_number",
            "TRUE AS initial_report",
        ]
    if feat["have_adverse_flag"]:
        select_cols.append("(upper(TRIM(COALESCE(m.ADVERSE_EVENT_FLAG, ''))) = 'Y') AS adverse_event_flag")
    else:
        select_cols.append("NULL::BOOLEAN AS adverse_event_flag")
    if feat["have_problem_flag"]:
        select_cols.append("(upper(TRIM(COALESCE(m.PRODUCT_PROBLEM_FLAG, ''))) = 'Y') AS product_problem_flag")
    else:
        select_cols.append("NULL::BOOLEAN AS product_problem_flag")

    select_cols += [
        "df.BRAND_NAME",
        "df.GENERIC_NAME",
        "df.MODEL_NUMBER",
        "UPPER(TRIM(COALESCE(df.MANUFACTURER_D_NAME, m.MANUFACTURER_NAME))) AS manufacturer",
        "UPPER(TRIM(df.DEVICE_REPORT_PRODUCT_CODE)) AS product_code",
        "COALESCE(dc.device_count, 0) AS device_count",
    ]
    joins = [
        "LEFT JOIN _x_device_first df USING (MDR_REPORT_KEY)",
        "LEFT JOIN _x_device_count dc USING (MDR_REPORT_KEY)",
    ]

    if feat["have_device_age"]:
        select_cols.append(
            f"({DEVICE_AGE_SQL.replace('DEVICE_AGE_TEXT', 'df.DEVICE_AGE_TEXT')}) AS device_age_days"
        )
        select_cols.append("df.DEVICE_AGE_TEXT AS device_age_text_raw")
    else:
        select_cols += [
            "NULL::DOUBLE AS device_age_days",
            "NULL::VARCHAR AS device_age_text_raw",
        ]
    if feat["have_implant"]:
        select_cols.append(
            "(upper(TRIM(COALESCE(df.IMPLANT_FLAG, ''))) = 'Y') AS implant_flag"
        )
    else:
        select_cols.append("NULL::BOOLEAN AS implant_flag")
    if feat["have_dev_operator"]:
        select_cols.append("df.DEVICE_OPERATOR AS device_operator")
    else:
        select_cols.append("NULL::VARCHAR AS device_operator")
    if feat["have_dev_eval"]:
        select_cols.append(
            "(upper(TRIM(COALESCE(df.DEVICE_EVALUATED_BY_MANUFACTURER, ''))) = 'Y') "
            "AS device_evaluated_by_manufacturer"
        )
    else:
        select_cols.append("NULL::BOOLEAN AS device_evaluated_by_manufacturer")
    if feat["have_reproc"]:
        select_cols.append(
            "(upper(TRIM(COALESCE(df.REPROCESSED_AND_REUSED_FLAG, ''))) = 'Y') "
            "AS reprocessed_and_reused"
        )
    else:
        select_cols.append("NULL::BOOLEAN AS reprocessed_and_reused")

    if feat["have_patient"]:
        joins.append("LEFT JOIN _x_patient_agg pa USING (MDR_REPORT_KEY)")
        select_cols += [
            "COALESCE(pa.patient_count, 0) AS patient_count",
            "pa.age_years_min",
            "pa.age_years_max",
            "pa.age_years_avg",
            "pa.sex_list",
        ]
        if feat["have_outcome"]:
            wrap = "',' || COALESCE(pa.outcome_codes_raw,'') || ','"
            for code, label in OUTCOME_LABELS.items():
                flag_col = f"outcome_{label}"
                select_cols.append(
                    f"({wrap} ILIKE '%,{code},%' OR "
                    f" {wrap} ILIKE '%,{code} ,%' OR "
                    f" {wrap} ILIKE '%, {code},%') AS {flag_col}"
                )
            serious_codes = ['D', 'L', 'H', 'S', 'C', 'R']
            serious_terms = " OR ".join(f"{wrap} ILIKE '%,{c},%'" for c in serious_codes)
            select_cols.append(f"({serious_terms}) AS any_serious_outcome")
            select_cols.append("pa.outcome_codes_raw")
        else:
            for code, label in OUTCOME_LABELS.items():
                select_cols.append(f"NULL::BOOLEAN AS outcome_{label}")
            select_cols.append("NULL::BOOLEAN AS any_serious_outcome")
            select_cols.append("NULL::VARCHAR AS outcome_codes_raw")
    else:
        select_cols += [
            "0 AS patient_count",
            "NULL::DOUBLE AS age_years_min",
            "NULL::DOUBLE AS age_years_max",
            "NULL::DOUBLE AS age_years_avg",
            "NULL::VARCHAR AS sex_list",
        ]
        for code, label in OUTCOME_LABELS.items():
            select_cols.append(f"NULL::BOOLEAN AS outcome_{label}")
        select_cols.append("NULL::BOOLEAN AS any_serious_outcome")
        select_cols.append("NULL::VARCHAR AS outcome_codes_raw")

    if feat["have_fdp"]:
        joins.append("LEFT JOIN _x_dev_prob_agg dpa USING (MDR_REPORT_KEY)")
        select_cols.append("dpa.codes AS device_problem_codes")
    else:
        select_cols.append("NULL::VARCHAR AS device_problem_codes")

    if feat["have_ppc"]:
        joins.append("LEFT JOIN _x_pat_prob_agg ppa USING (MDR_REPORT_KEY)")
        select_cols.append("ppa.codes AS patient_problem_codes")
    else:
        select_cols.append("NULL::VARCHAR AS patient_problem_codes")

    if feat["have_foi"]:
        joins.append("LEFT JOIN _x_narrative_agg na USING (MDR_REPORT_KEY)")
        select_cols += [
            "COALESCE(na.narr_part_count, 0) > 0 AS has_narrative",
            "na.narr_part_count",
            "na.narrative_desc",
            "na.narrative_mfg",
        ]
    else:
        select_cols += [
            "FALSE AS has_narrative",
            "0 AS narr_part_count",
            "NULL::VARCHAR AS narrative_desc",
            "NULL::VARCHAR AS narrative_mfg",
        ]

    select_cols += [
        "LOWER(df.BRAND_NAME)   AS brand_name_l",
        "LOWER(df.GENERIC_NAME) AS generic_name_l",
        "LOWER(df.MODEL_NUMBER) AS model_number_l",
        "LOWER(COALESCE(df.MANUFACTURER_D_NAME, m.MANUFACTURER_NAME)) AS manufacturer_l",
        ("LOWER(na.narrative_desc) AS narrative_desc_l"
         if feat["have_foi"] else "NULL::VARCHAR AS narrative_desc_l"),
    ]

    return select_cols, joins


def compose_mdr_flat(con: duckdb.DuckDBPyConnection, feat: dict) -> int:
    select_cols, joins = build_select_and_joins(feat)

    info("   -> Creating empty mdr_flat with full schema")
    con.execute("DROP TABLE IF EXISTS mdr_flat;")
    schema_sql = (
        "CREATE TABLE mdr_flat AS SELECT "
        + ",\n  ".join(select_cols)
        + " FROM mdr m " + " ".join(joins)
        + " WHERE 1=0;"
    )
    con.execute(schema_sql)

    info("   -> Discovering year range")
    years_df = con.execute("""
        SELECT DISTINCT CAST(strftime(DATE_PREF, '%Y') AS INTEGER) AS y
        FROM mdr
        WHERE DATE_PREF IS NOT NULL
        ORDER BY y;
    """).fetchall()
    years = [int(r[0]) for r in years_df if r[0] is not None]
    info(f"      = {len(years)} years: {years[0] if years else '?'} to {years[-1] if years else '?'}")

    cols_sql = ",\n  ".join(select_cols)
    joins_sql = " ".join(joins)

    total = 0
    for year in years:
        t0 = time.time()
        insert_sql = (
            f"INSERT INTO mdr_flat SELECT {cols_sql} "
            f"FROM mdr m {joins_sql} "
            f"WHERE CAST(strftime(m.DATE_PREF, '%Y') AS INTEGER) = {year};"
        )
        try:
            con.execute(insert_sql)
            n = con.execute(
                "SELECT COUNT(*) FROM mdr_flat WHERE report_year = ?", [year]
            ).fetchone()[0]
            total += n
            info(f"   -> Year {year}: {n:>10,} rows ({(time.time()-t0):.1f}s)  total: {total:,}")
        except duckdb.OutOfMemoryException as e:
            info(f"   ! OOM on year {year}: {e}")
            info(f"   ! Try setting memory_limit lower in configure_for_memory().")
            raise

    t0 = time.time()
    null_insert = (
        f"INSERT INTO mdr_flat SELECT {cols_sql} "
        f"FROM mdr m {joins_sql} WHERE m.DATE_PREF IS NULL;"
    )
    try:
        con.execute(null_insert)
        n = con.execute("SELECT COUNT(*) FROM mdr_flat WHERE report_year IS NULL").fetchone()[0]
        if n > 0:
            info(f"   -> NULL-date rows: {n:,} ({(time.time()-t0):.1f}s)")
            total += n
    except Exception as e:
        info(f"   ! Could not insert NULL-date rows: {e}")

    info(f"   = mdr_flat total: {total:,} rows")
    return total


def build_rollups(con: duckdb.DuckDBPyConnection) -> None:
    info("=> Building rollup tables ...")

    con.execute("""
        CREATE OR REPLACE TABLE agg_yearly_event AS
        SELECT report_year, EVENT_TYPE,
               COUNT(*)::BIGINT AS n,
               SUM(CASE WHEN IS_RWD_SOURCED THEN 1 ELSE 0 END) AS n_rwd,
               SUM(CASE WHEN initial_report THEN 1 ELSE 0 END) AS n_initial,
               SUM(CASE WHEN is_supplement THEN 1 ELSE 0 END) AS n_supplement
        FROM mdr_flat
        WHERE report_year IS NOT NULL
        GROUP BY 1, 2;
    """)

    if has_col(con, "mdr_flat", "any_serious_outcome"):
        con.execute("""
            CREATE OR REPLACE TABLE agg_yearly_outcomes AS
            SELECT report_year,
                   COUNT(*)::BIGINT AS n,
                   SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS n_death,
                   SUM(CASE WHEN outcome_life_threatening THEN 1 ELSE 0 END) AS n_life_threat,
                   SUM(CASE WHEN outcome_hospitalization THEN 1 ELSE 0 END) AS n_hosp,
                   SUM(CASE WHEN outcome_disability THEN 1 ELSE 0 END) AS n_disability,
                   SUM(CASE WHEN outcome_required_intervention THEN 1 ELSE 0 END) AS n_intervention,
                   SUM(CASE WHEN outcome_congenital_anomaly THEN 1 ELSE 0 END) AS n_congenital,
                   SUM(CASE WHEN outcome_other THEN 1 ELSE 0 END) AS n_other,
                   SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious
            FROM mdr_flat
            WHERE report_year IS NOT NULL
            GROUP BY 1;
        """)

    con.execute("""
        CREATE OR REPLACE TABLE agg_product_code AS
        SELECT product_code,
               COUNT(*)::BIGINT AS n_reports,
               SUM(CASE WHEN EVENT_TYPE = 'D'  THEN 1 ELSE 0 END) AS n_deaths,
               SUM(CASE WHEN EVENT_TYPE = 'IN' THEN 1 ELSE 0 END) AS n_injuries,
               SUM(CASE WHEN EVENT_TYPE = 'M'  THEN 1 ELSE 0 END) AS n_malfunc,
               MIN(report_year) AS first_year,
               MAX(report_year) AS last_year
        FROM mdr_flat
        WHERE product_code IS NOT NULL AND product_code <> ''
        GROUP BY 1;
    """)

    con.execute("""
        CREATE OR REPLACE TABLE agg_manufacturer AS
        SELECT manufacturer,
               COUNT(*)::BIGINT AS n_reports,
               SUM(CASE WHEN EVENT_TYPE = 'D'  THEN 1 ELSE 0 END) AS n_deaths,
               SUM(CASE WHEN EVENT_TYPE = 'IN' THEN 1 ELSE 0 END) AS n_injuries,
               SUM(CASE WHEN EVENT_TYPE = 'M'  THEN 1 ELSE 0 END) AS n_malfunc
        FROM mdr_flat
        WHERE manufacturer IS NOT NULL AND manufacturer <> ''
        GROUP BY 1;
    """)

    if has_col(con, "mdr_flat", "reporter_country_code"):
        con.execute("""
            CREATE OR REPLACE TABLE agg_country AS
            SELECT reporter_country_code AS country, COUNT(*)::BIGINT AS n
            FROM mdr_flat
            WHERE reporter_country_code IS NOT NULL
              AND reporter_country_code <> ''
            GROUP BY 1;
        """)

    if has_col(con, "mdr_flat", "device_problem_codes"):
        con.execute("""
            CREATE OR REPLACE TABLE flat_dev_problems AS
            SELECT MDR_REPORT_KEY, TRIM(c.value) AS code
            FROM mdr_flat,
                 unnest(string_split(COALESCE(device_problem_codes, ''), ',')) c(value)
            WHERE TRIM(c.value) <> '';
        """)
        con.execute(
            "CREATE OR REPLACE TABLE agg_dev_problems_global AS "
            "SELECT code, COUNT(DISTINCT MDR_REPORT_KEY)::BIGINT AS n "
            "FROM flat_dev_problems GROUP BY 1;"
        )

    if has_col(con, "mdr_flat", "patient_problem_codes"):
        con.execute("""
            CREATE OR REPLACE TABLE flat_pat_problems AS
            SELECT MDR_REPORT_KEY, TRIM(c.value) AS code
            FROM mdr_flat,
                 unnest(string_split(COALESCE(patient_problem_codes, ''), ',')) c(value)
            WHERE TRIM(c.value) <> '';
        """)
        con.execute(
            "CREATE OR REPLACE TABLE agg_pat_problems_global AS "
            "SELECT code, COUNT(DISTINCT MDR_REPORT_KEY)::BIGINT AS n "
            "FROM flat_pat_problems GROUP BY 1;"
        )

    for tbl in [
        "agg_yearly_event", "agg_yearly_outcomes", "agg_product_code",
        "agg_manufacturer", "agg_country",
        "flat_dev_problems", "agg_dev_problems_global",
        "flat_pat_problems", "agg_pat_problems_global",
    ]:
        if table_exists(con, tbl):
            n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            info(f"   = {tbl:30s}  {n:>12,}")


def create_indexes(con: duckdb.DuckDBPyConnection) -> None:
    info("=> Indexing analytic tables ...")
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_mf_key       ON mdr_flat(MDR_REPORT_KEY)",
        "CREATE INDEX IF NOT EXISTS idx_mf_year      ON mdr_flat(report_year)",
        "CREATE INDEX IF NOT EXISTS idx_mf_pc        ON mdr_flat(product_code)",
        "CREATE INDEX IF NOT EXISTS idx_mf_event     ON mdr_flat(EVENT_TYPE)",
        "CREATE INDEX IF NOT EXISTS idx_mf_date      ON mdr_flat(DATE_PREF)",
        "CREATE INDEX IF NOT EXISTS idx_mf_mfg       ON mdr_flat(manufacturer)",
        "CREATE INDEX IF NOT EXISTS idx_mf_rep_num   ON mdr_flat(REPORT_NUMBER)",
    ]
    if table_exists(con, "flat_dev_problems"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_fdp_key  ON flat_dev_problems(MDR_REPORT_KEY)",
            "CREATE INDEX IF NOT EXISTS idx_fdp_code ON flat_dev_problems(code)",
        ]
    if table_exists(con, "flat_pat_problems"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_fpp_key  ON flat_pat_problems(MDR_REPORT_KEY)",
            "CREATE INDEX IF NOT EXISTS idx_fpp_code ON flat_pat_problems(code)",
        ]
    for s in stmts:
        try:
            con.execute(s)
        except Exception as e:
            info(f"   ! {e}")


def cleanup_intermediates(con: duckdb.DuckDBPyConnection) -> None:
    info("=> Dropping temporary intermediates ...")
    for tbl in ["_x_device_first", "_x_device_count", "_x_patient_agg",
                "_x_dev_prob_agg", "_x_pat_prob_agg", "_x_narrative_agg"]:
        try:
            con.execute(f"DROP TABLE IF EXISTS {tbl};")
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MAUDE analytic build v2 (memory-safe)")
    ap.add_argument("--db", default="maude_final.duckdb")
    ap.add_argument("--keep-intermediates", action="store_true",
                    help="Don't drop _x_* tables after build (for debugging)")
    args = ap.parse_args(argv)

    t0 = time.time()
    con = duckdb.connect(args.db)

    info("MAUDE analytic build v2 (memory-safe, year-chunked)")
    info(f"  db = {args.db}")
    info("")

    if not table_exists(con, "mdr") or not table_exists(con, "device"):
        info("ERROR: required tables mdr/device not found.")
        return 2

    configure_for_memory(con)
    info("")

    info("=> Building per-MDR aggregate intermediates ...")
    feat = build_intermediates(con)
    if not feat.get("have_date_pref"):
        return 1
    info("")

    info("=> Composing mdr_flat (one year per INSERT)")
    n = compose_mdr_flat(con, feat)
    if n == 0:
        return 1
    info("")

    build_rollups(con)
    create_indexes(con)

    if not args.keep_intermediates:
        cleanup_intermediates(con)

    info("")
    info(f"Done in {(time.time() - t0) / 60:.1f} min")
    info("")
    info("Launch v4 dashboard:")
    info("  streamlit run maude_dashboard_v4.py")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
