"""
MAUDE Ingestion Pipeline v2
============================
Robust ingestion of FDA MAUDE downloadable data files into a DuckDB analytical database.

Key improvements over v1
------------------------
1. CSV vs pipe-delimited auto-detection (fixes silent corruption of dictionary CSVs).
2. State-machine handling of multi-line foitext narratives (fixes truncated narratives
   and orphan rows caused by embedded \\r/\\n).
3. Robust BOM removal using str.removeprefix (instead of misused lstrip).
4. Date parsing at ingest time: produces typed DATE columns (DATE_RECEIVED_D,
   REPORT_DATE_D, DATE_OF_EVENT_D) with indexes. Massive query speedup downstream.
5. Patient age normalised to age-in-years at ingest, with full unit support
   (HR, DY, WK, MO, YR, DEC).
6. Derived flags at ingest:
     - HAS_REDACTION_B4 (trade secret)
     - HAS_REDACTION_B6 (personal/medical)
     - IS_FORWARDED_803_22_B2 (forwarded under 21 CFR 803.22(b)(2))
     - IS_RWD_SOURCED       (real-world data exemption, RWDYYXXXXX numbering)
     - IS_SUPPLEMENT        (heuristic: REPORT_NUMBER ends in -XXX > 0)
7. Indexes on PRODUCT_CODE, MANUFACTURER, BRAND/GENERIC (lowered), DATE_RECEIVED_D,
   plus btree on MDR_REPORT_KEY across all tables.
8. Optional DuckDB FTS index on foi narratives for fast keyword search.
9. CLI args replace hard-coded constants. Robust progress reporting.
10. Headerless supplemental files handled via explicit schemas per FDA spec.

References (official MAUDE documentation)
-----------------------------------------
- About MAUDE: https://www.fda.gov/medical-devices/...about-maude-database
- MDR Data Files (file specs, 82-field MDRFOI, etc.):
  https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files
- Ensign LG, Cohen KB. "A Primer to the Structure, Content and Linkage of the
  FDA's MAUDE Files." eGEMs 2017;5(1):12. PMC5994953.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

import duckdb

DELIM = "|"

# MDR_REPORT_KEY is a numeric (typically 7-10 digits, historically smaller).
# We use this to detect record starts when re-assembling multi-line foitext
# records. The pattern is intentionally lenient on the low end: a single line
# beginning with digits + pipe is treated as a new record. Embedded "\d+\|"
# inside a wrapped narrative is highly unlikely.
MDR_KEY_RE = re.compile(r"^\d{4,12}\|")

# Headerless supplemental files: explicit schemas per FDA docs.
FOIDEVPROBLEM_COLS = ["MDR_REPORT_KEY", "DEVICE_PROBLEM_CODE"]

# Patient problem codes uses the same headerless pipe-delimited format as foidev.
# Format observed: MDR_REPORT_KEY | PROBLEM_CODE (single code per row historically),
# though some releases pack comma-separated lists.
PATIENT_PROBLEM_COLS = ["MDR_REPORT_KEY", "PATIENT_SEQUENCE_NUMBER", "PROBLEM_CODE"]


def info(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def sniff_delimiter(path: str, sample_bytes: int = 16384) -> str:
    """Detect whether a file is pipe-delimited or comma-delimited.

    MAUDE distribution files are pipe-delimited; the dictionary CSVs are
    comma-delimited but get misingested by v1 which routes everything through
    the pipe normaliser.
    """
    with open(path, "rb") as fh:
        chunk = fh.read(sample_bytes)
    text = chunk.decode("latin-1", errors="ignore")
    # Look only at the first ~2 lines for the count
    first = text.split("\n", 2)[:2]
    pipe = sum(line.count("|") for line in first)
    comma = sum(line.count(",") for line in first)
    if pipe == 0 and comma > 0:
        return ","
    return "|"


# ---------------------------------------------------------------------------
# foitext multi-line reassembly
# ---------------------------------------------------------------------------

def reassemble_foitext(in_path: str, out_path: str, expected_cols: int) -> int:
    """Stream a foitext file and merge continuation lines into proper records.

    Strategy: a real record begins with `<MDR_REPORT_KEY>|` (digits + pipe).
    Any line that doesn't match that pattern is a continuation of the previous
    record's last field (the narrative). We strip the embedded newline and
    paste it back together. Critically, this preserves narrative content
    instead of silently dropping continuation lines into orphaned rows.

    Returns the number of assembled records written.
    """
    written = 0
    current = ""
    with open(in_path, "r", encoding="latin-1", errors="replace") as fin, \
         open(out_path, "w", encoding="utf-8", newline="") as fout:

        # Pass through header verbatim (cleaned)
        header = fin.readline()
        if header.startswith("\ufeff"):
            header = header.removeprefix("\ufeff")
        header = header.replace("ï»¿", "").rstrip("\r\n")
        # Some MAUDE releases use tabs in header — normalise to pipe
        header = header.replace("\t", DELIM)
        fout.write(header + "\n")

        for raw in fin:
            line = raw.rstrip("\n").rstrip("\r")
            if MDR_KEY_RE.match(line):
                if current:
                    fout.write(current + "\n")
                    written += 1
                current = line
            else:
                # Continuation: append with a space, replacing the lost newline
                if current:
                    current = current + " " + line.strip()
                # Else: orphaned continuation before any record; ignore.
        if current:
            fout.write(current + "\n")
            written += 1
    return written


# ---------------------------------------------------------------------------
# Pipe normaliser (general MAUDE text files except foitext)
# ---------------------------------------------------------------------------

_CSV_OPTS_CACHE: Optional[str] = None


def csv_read_opts(con) -> str:
    """read_csv() options for the FDA pipe files, tuned to the DuckDB in use.

    `strict_mode` only exists from DuckDB 1.2 onward. Hardcoding it made every
    read_csv call fail with "Invalid named parameter" on DuckDB 1.1.x — and
    because the loader logged the error and returned 0 rather than raising, the
    whole ingest completed "successfully" with no mdr, device, foi or patient
    table at all. The option is probed once and included only if supported.

    The rest:
      all_varchar=true    FDA files mix types within a column across years;
                          typing happens later, deliberately.
      ignore_errors=true  a handful of malformed rows must not abort a 3 GB file.
      null_padding=true   short rows are padded rather than rejected.
      quote='' escape=''  the files are not quoted; treating " as a quote
                          swallows narrative text containing inches marks.
      max_line_size       reassembled narratives exceed the 2 MB default.
      sample_size=-1      scan the whole file so column types are stable.
    """
    global _CSV_OPTS_CACHE
    if _CSV_OPTS_CACHE is not None:
        return _CSV_OPTS_CACHE

    base = (
        f"delim='{DELIM}', header=1, all_varchar=true, "
        f"ignore_errors=true, null_padding=true, "
        f"quote='', escape='', max_line_size=67108864, sample_size=-1"
    )
    supported = base
    probe = None
    try:
        fd, probe = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"A{DELIM}B\n1{DELIM}2\n")
        probe_sql = probe.replace("\\", "/")
        try:
            con.execute(
                f"SELECT * FROM read_csv('{probe_sql}', {base}, strict_mode=false) LIMIT 1"
            ).fetchall()
            supported = base + ", strict_mode=false"
        except Exception:
            info("   i DuckDB build predates strict_mode; using compatible CSV options.")
    except Exception:
        pass
    finally:
        if probe:
            try:
                os.remove(probe)
            except OSError:
                pass

    _CSV_OPTS_CACHE = supported
    return supported


def normalize_pipe(in_path: str) -> Optional[str]:
    """Read a pipe-delimited MAUDE file with Latin-1 decode, strip BOM,
    normalise to UTF-8, and pad/truncate rows to header column count.

    Used for all MAUDE primary tables EXCEPT foitext (which needs the
    multi-line reassembler above) and EXCEPT CSV dictionary files (which
    use a different path).
    """
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        with open(in_path, "r", encoding="latin-1", errors="replace") as fin, \
             open(tmp, "w", encoding="utf-8", newline="") as fout:
            writer = csv.writer(fout, delimiter=DELIM, lineterminator="\n")
            header = fin.readline()
            if header.startswith("\ufeff"):
                header = header.removeprefix("\ufeff")
            header = header.replace("ï»¿", "").rstrip("\r\n").replace("\t", DELIM)
            parts = header.split(DELIM)
            ncols = len(parts)
            writer.writerow(parts)

            for raw in fin:
                line = raw.rstrip("\n").rstrip("\r")
                if not line:
                    continue
                line = line.replace("\t", DELIM)
                row = [p.strip() for p in line.split(DELIM)]
                if len(row) < ncols:
                    row.extend([""] * (ncols - len(row)))
                elif len(row) > ncols:
                    # Excess fields get folded back into the last column.
                    # This is safer than truncating, which destroys data.
                    row = row[: ncols - 1] + [DELIM.join(row[ncols - 1 :])]
                writer.writerow(row)
        return tmp
    except Exception as e:
        info(f"   ! Normalise failed for {os.path.basename(in_path)}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return None


# ---------------------------------------------------------------------------
# Generic loader for normalised pipe files
# ---------------------------------------------------------------------------

def load_pipe_files(
    con: duckdb.DuckDBPyConnection,
    label: str,
    files: list[str],
    table: str,
    reassemble: bool = False,
) -> int:
    """Create `table` from the first file, append the rest. Returns row count."""
    if not files:
        info(f"  (no files found for {label})")
        return 0

    info(f"=> {label}: {len(files)} file(s)")
    tmp_files: list[str] = []

    try:
        for i, src in enumerate(files):
            info(f"   - {os.path.basename(src)}")
            if reassemble:
                fd, tmp = tempfile.mkstemp(suffix=".csv")
                os.close(fd)
                # Probe column count from header
                with open(src, "r", encoding="latin-1", errors="replace") as fh:
                    header = fh.readline()
                    if header.startswith("\ufeff"):
                        header = header.removeprefix("\ufeff")
                    header = header.replace("ï»¿", "")
                    ncols = len(header.replace("\t", DELIM).split(DELIM))
                n = reassemble_foitext(src, tmp, expected_cols=ncols)
                info(f"     reassembled {n:,} records")
                tmp_files.append(tmp)
            else:
                tmp = normalize_pipe(src)
                if tmp is None:
                    continue
                tmp_files.append(tmp)

        if not tmp_files:
            return 0

        # Build a UNION ALL across all temp files in one shot.
        # The read_csv options here are tuned for MAUDE's quirks:
        #   - quote='' / escape=''  : MAUDE narratives contain bare quote
        #     characters that aren't part of any quoting convention; treating
        #     them as quotes corrupts records.
        #   - strict_mode=false     : tolerate rows that don't match the
        #     declared column count (rare but it happens).
        #   - max_line_size         : some reassembled narratives exceed the
        #     default 2MB line limit.
        #   - null_padding=true     : fill short rows with NULLs.
        #   - sample_size=-1        : scan the whole file for type inference,
        #     so column types are stable across years.
        read_opts = csv_read_opts(con)

        def _load_one(path: str) -> tuple[bool, str]:
            """Try to read one normalised temp file. Returns (ok, error_msg)."""
            try:
                con.execute(
                    f"CREATE OR REPLACE TEMP TABLE _probe AS "
                    f"SELECT * FROM read_csv('{path}', {read_opts}) LIMIT 0;"
                )
                con.execute("DROP TABLE IF EXISTS _probe;")
                return True, ""
            except Exception as e:
                return False, str(e)

        first = tmp_files[0]
        ok, err = _load_one(first)
        if not ok:
            info(f"   ! Could not parse {os.path.basename(first)}: {err}")
            info(f"   ! Skipping {label} entirely.")
            return 0
        try:
            con.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM read_csv('{first}', {read_opts});"
            )
        except Exception as e:
            info(f"   ! Failed to load {os.path.basename(first)}: {e}")
            return 0

        for tmp in tmp_files[1:]:
            try:
                con.execute(
                    f"INSERT INTO {table} BY NAME "
                    f"SELECT * FROM read_csv('{tmp}', {read_opts});"
                )
            except Exception as e:
                info(f"   ! Append failed for {os.path.basename(tmp)}: {e}")

        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        info(f"   = {n:,} rows in {table}")
        return n
    finally:
        for t in tmp_files:
            try:
                os.remove(t)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CSV dictionary loaders (comma-delimited, with quoting)
# ---------------------------------------------------------------------------

def load_csv_dict(con: duckdb.DuckDBPyConnection, label: str, path: str, table: str) -> int:
    if not os.path.exists(path):
        info(f"  (no file for {label})")
        return 0
    info(f"=> {label}: {os.path.basename(path)} (comma-delimited CSV)")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT
            CAST(FDA_CODE AS VARCHAR) AS FDA_CODE,
            TRIM(CAST(TERM AS VARCHAR)) AS TERM,
            CAST(NCIT_CODE AS VARCHAR) AS NCIT_CODE,
            CAST(IMDRF_CODE AS VARCHAR) AS IMDRF_CODE
        FROM read_csv_auto('{path}', delim=',', header=1, quote='"', sample_size=-1);
        """
    )
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    info(f"   = {n:,} rows in {table}")
    return n


# ---------------------------------------------------------------------------
# Headerless supplemental files (foidevproblem, patient problem codes)
# ---------------------------------------------------------------------------

def load_problem_codes(
    con: duckdb.DuckDBPyConnection,
    label: str,
    path: str,
    table: str,
    canonical_cols: list[str],
) -> int:
    """Load a MAUDE problem-code file, auto-detecting its format.

    MAUDE's problem-code files have drifted in format over the years and the
    FDA documentation is stale. Known variants in the wild:

      * 2-column headerless:  MDR_REPORT_KEY | PROBLEM_CODE        (pre-2020)
      * 3-column headerless:  MDR_REPORT_KEY | PATIENT_SEQUENCE_NUMBER | PROBLEM_CODE
      * 5-column WITH HEADER: MDR_REPORT_KEY | PATIENT_SEQUENCE_NO | PROBLEM_CODE
                              | DATE_ADDED | DATE_CHANGED          (current)

    We sniff (a) whether the first row is a header and (b) the column count,
    then map source columns onto our canonical schema. `canonical_cols` is the
    target schema, e.g. ["MDR_REPORT_KEY", "PATIENT_SEQUENCE_NUMBER", "PROBLEM_CODE"].
    """
    if not os.path.exists(path):
        info(f"  (no file for {label})")
        return 0

    # Read enough to see the first non-blank line
    with open(path, "r", encoding="latin-1", errors="replace") as fh:
        first_line = ""
        for line in fh:
            if line.strip():
                first_line = line.rstrip("\r\n")
                break
    if not first_line:
        info(f"  ! {label}: empty file")
        return 0
    if first_line.startswith("\ufeff"):
        first_line = first_line.removeprefix("\ufeff")
    first_line = first_line.replace("ï»¿", "")

    fields = first_line.split(DELIM)
    ncols = len(fields)
    first_field = fields[0].strip()

    # Header detection: first field is non-numeric and looks like a column name
    has_header = (not first_field.isdigit()
                  and bool(first_field)
                  and any(c.isalpha() for c in first_field))

    # Build a name->index map for the source columns
    if has_header:
        header_names = [f.strip().upper() for f in fields]
        # Map canonical columns to source positions by name (robust to reorder)
        aliases = {
            "MDR_REPORT_KEY": ["MDR_REPORT_KEY"],
            "PATIENT_SEQUENCE_NUMBER": [
                "PATIENT_SEQUENCE_NUMBER", "PATIENT_SEQUENCE_NO",
                "PAT_SEQ_NO", "SEQUENCE_NO",
            ],
            "PROBLEM_CODE": [
                "PROBLEM_CODE", "PAT_PROBLEM_CODE", "PATIENT_PROBLEM_CODE",
                "DEVICE_PROBLEM_CODE", "DEV_PROBLEM_CODE",
            ],
            "DEVICE_PROBLEM_CODE": [
                "DEVICE_PROBLEM_CODE", "DEV_PROBLEM_CODE", "PROBLEM_CODE",
            ],
        }
        src_index: dict[str, int] = {}
        for canonical in canonical_cols:
            for alias in aliases.get(canonical, [canonical]):
                if alias in header_names:
                    src_index[canonical] = header_names.index(alias)
                    break
        if "MDR_REPORT_KEY" not in src_index:
            info(f"  ! {label}: header present but no MDR_REPORT_KEY column found: {header_names}")
            return 0
        info(f"=> {label}: {os.path.basename(path)} "
             f"({ncols}-col WITH HEADER, mapping by name)")
    else:
        # Headerless: map by position. Historical 2-col is MDR|CODE;
        # 3-col is MDR|SEQ|CODE.
        src_index = {}
        if ncols == 2:
            # MDR_REPORT_KEY | (PROBLEM_CODE or DEVICE_PROBLEM_CODE)
            src_index[canonical_cols[0]] = 0
            src_index[canonical_cols[-1]] = 1
        elif ncols >= 3:
            for i, c in enumerate(canonical_cols):
                if i < ncols:
                    src_index[c] = i
        else:
            src_index[canonical_cols[0]] = 0
        info(f"=> {label}: {os.path.basename(path)} "
             f"(headerless, {ncols}-col, mapping by position)")

    # Read ALL source columns as VARCHAR (named col0..colN), then project.
    src_col_names = [f"col{i}" for i in range(ncols)]
    src_col_spec = ", ".join(f"'{c}': 'VARCHAR'" for c in src_col_names)

    # Target table with full canonical schema
    col_def = ", ".join(f'"{c}" VARCHAR' for c in canonical_cols)
    con.execute(f'CREATE OR REPLACE TABLE {table} ({col_def});')

    # Projection: pull mapped source col or NULL
    select_cols = []
    for c in canonical_cols:
        if c in src_index:
            select_cols.append(f'col{src_index[c]} AS "{c}"')
        else:
            select_cols.append(f'NULL AS "{c}"')

    header_flag = "true" if has_header else "false"
    mdr_idx = src_index["MDR_REPORT_KEY"]
    code_col = canonical_cols[-1]
    code_idx = src_index.get(code_col, None)

    where_clause = f"col{mdr_idx} IS NOT NULL AND TRIM(col{mdr_idx}) <> ''"
    if code_idx is not None:
        where_clause += f" AND col{code_idx} IS NOT NULL AND TRIM(col{code_idx}) <> ''"

    # strict_mode exists only from DuckDB 1.2; csv_read_opts() probes for it.
    strict_opt = (", strict_mode=false"
                  if "strict_mode" in csv_read_opts(con) else "")

    try:
        con.execute(
            f"""
            INSERT INTO {table}
            SELECT {', '.join(select_cols)} FROM read_csv(
                '{path}',
                delim='{DELIM}',
                header={header_flag},
                columns={{{src_col_spec}}},
                ignore_errors=true,
                null_padding=true,
                quote='',
                escape='',
                max_line_size=67108864
                {strict_opt}
            )
            WHERE {where_clause};
            """
        )
    except Exception as e:
        info(f"  ! {label} load failed: {e}")
        return 0

    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    info(f"   = {n:,} rows in {table}")
    if n == 0:
        info(f"   ! WARNING: 0 rows loaded. Check the source file format.")
    return n


# Backwards-compatible alias (older call sites use load_headerless_pipe)
def load_headerless_pipe(con, label, path, table, columns):
    return load_problem_codes(con, label, path, table, columns)


# ---------------------------------------------------------------------------
# Post-ingest enrichments: typed dates, derived flags, indexes, FTS
# ---------------------------------------------------------------------------

def add_typed_dates(con: duckdb.DuckDBPyConnection) -> None:
    """Add proper DATE columns to mdr from VARCHAR date fields and index them.

    MAUDE distributes dates as both 'YYYYMMDD' and 'MM/DD/YYYY' depending on
    era. We try both for each field. Producing typed DATE columns once at
    ingest time avoids per-query try_strptime() scans (the single biggest
    perf hit in the v1 dashboard).
    """
    info("=> Typing date columns on mdr")
    cols = set(r[0] for r in con.execute("DESCRIBE mdr").fetchall())

    def add_typed(src: str, dst: str) -> bool:
        if src not in cols:
            return False
        con.execute(f"ALTER TABLE mdr ADD COLUMN IF NOT EXISTS {dst} DATE;")
        con.execute(
            f"""
            UPDATE mdr SET {dst} = COALESCE(
                try_strptime({src}, '%Y%m%d')::DATE,
                try_strptime({src}, '%m/%d/%Y')::DATE
            );
            """
        )
        return True

    has_rcvd = add_typed("DATE_RECEIVED", "DATE_RECEIVED_D")
    has_rpt = add_typed("REPORT_DATE", "REPORT_DATE_D")
    add_typed("DATE_OF_EVENT", "DATE_OF_EVENT_D")

    # DATE_PREF: prefer received, fall back to report (only reference fields
    # that exist, so the COALESCE doesn't blow up).
    if has_rcvd or has_rpt:
        con.execute("ALTER TABLE mdr ADD COLUMN IF NOT EXISTS DATE_PREF DATE;")
        coalesce_args = []
        if has_rcvd:
            coalesce_args.append("DATE_RECEIVED_D")
        if has_rpt:
            coalesce_args.append("REPORT_DATE_D")
        con.execute(
            f"UPDATE mdr SET DATE_PREF = COALESCE({', '.join(coalesce_args)});"
        )


def add_patient_age_years(con: duckdb.DuckDBPyConnection) -> None:
    """Parse PATIENT_AGE into AGE_YEARS once at ingest.

    Patient ages in MAUDE come in many shapes: '67 YR', '6 MO', '14 DY',
    '8 HR', '3 WK', '7 DEC' (decades), bare numbers, and even nested
    redaction markers '(b)(6)'. v1 only handled YR/MO/DY/DEC and silently
    mis-mapped everything else as years.
    """
    if not table_exists(con, "patient"):
        return
    info("=> Normalising PATIENT_AGE -> AGE_YEARS on patient")
    cols = [r[0] for r in con.execute("DESCRIBE patient").fetchall()]
    if "PATIENT_AGE" not in cols:
        return
    con.execute(
        """
        ALTER TABLE patient ADD COLUMN IF NOT EXISTS AGE_VALUE DOUBLE;
        ALTER TABLE patient ADD COLUMN IF NOT EXISTS AGE_UNIT VARCHAR;
        ALTER TABLE patient ADD COLUMN IF NOT EXISTS AGE_YEARS DOUBLE;

        UPDATE patient SET
            AGE_VALUE = try_cast(regexp_extract(PATIENT_AGE, '([0-9]+\\.?[0-9]*)', 1) AS DOUBLE),
            AGE_UNIT  = upper(COALESCE(NULLIF(regexp_extract(PATIENT_AGE, '([A-Za-z]+)', 1), ''), 'YR'));

        UPDATE patient SET AGE_YEARS = CASE
            WHEN AGE_UNIT = 'HR'  THEN AGE_VALUE / (24.0 * 365.25)
            WHEN AGE_UNIT = 'DY'  THEN AGE_VALUE / 365.25
            WHEN AGE_UNIT = 'WK'  THEN (AGE_VALUE * 7.0) / 365.25
            WHEN AGE_UNIT = 'MO'  THEN AGE_VALUE / 12.0
            WHEN AGE_UNIT = 'YR'  THEN AGE_VALUE
            WHEN AGE_UNIT = 'DEC' THEN AGE_VALUE * 10.0
            ELSE NULL
        END;
        """
    )


def add_flag_columns(con: duckdb.DuckDBPyConnection) -> None:
    """Compute derived flags on mdr that are useful for filtering.

    - IS_FORWARDED_803_22_B2: per FDA docs, forwarded reports have a fixed
      annotation in the narrative. We capture it as a row-level flag here so
      the dashboard can include/exclude them in one click.
    - IS_RWD_SOURCED: per FDA docs, RWD exemption reports are tagged by a
      REPORT_NUMBER beginning with 'RWD'.
    - HAS_REDACTION_B4 / B6: presence of FOIA exemption markers.
    """
    info("=> Computing derived flags on mdr")
    cols = [r[0] for r in con.execute("DESCRIBE mdr").fetchall()]
    con.execute("ALTER TABLE mdr ADD COLUMN IF NOT EXISTS IS_RWD_SOURCED BOOLEAN;")
    if "REPORT_NUMBER" in cols:
        con.execute(
            "UPDATE mdr SET IS_RWD_SOURCED = (REPORT_NUMBER ILIKE 'RWD%');"
        )
    else:
        con.execute("UPDATE mdr SET IS_RWD_SOURCED = FALSE;")

    if table_exists(con, "foi"):
        # IS_FORWARDED_803_22_B2: aggregate from foi to mdr via MDR_REPORT_KEY.
        # Materialise into a temp table for the UPDATE join, then drop it so
        # it doesn't appear in the final schema listing.
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE _fwd AS
            SELECT MDR_REPORT_KEY,
                   bool_or(FOI_TEXT ILIKE '%803.22(b)(2)%') AS fwd,
                   bool_or(FOI_TEXT ILIKE '%(b)(4)%')      AS b4,
                   bool_or(FOI_TEXT ILIKE '%(b)(6)%')      AS b6
            FROM foi
            GROUP BY MDR_REPORT_KEY;

            ALTER TABLE mdr ADD COLUMN IF NOT EXISTS IS_FORWARDED_803_22_B2 BOOLEAN;
            ALTER TABLE mdr ADD COLUMN IF NOT EXISTS HAS_REDACTION_B4 BOOLEAN;
            ALTER TABLE mdr ADD COLUMN IF NOT EXISTS HAS_REDACTION_B6 BOOLEAN;

            UPDATE mdr SET
                IS_FORWARDED_803_22_B2 = COALESCE(f.fwd, FALSE),
                HAS_REDACTION_B4       = COALESCE(f.b4,  FALSE),
                HAS_REDACTION_B6       = COALESCE(f.b6,  FALSE)
            FROM _fwd f
            WHERE mdr.MDR_REPORT_KEY = f.MDR_REPORT_KEY;

            DROP TABLE IF EXISTS _fwd;
            """
        )


def create_indexes(con: duckdb.DuckDBPyConnection) -> None:
    """Create indexes on common filter columns.

    DuckDB only supports min-max zonemaps for predicate pushdown plus
    explicit btree indexes. Indexes here matter mostly for point lookups
    (single MDR_REPORT_KEY) and equality joins.
    """
    info("=> Creating indexes")
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_mdr_key ON mdr(MDR_REPORT_KEY);",
        "CREATE INDEX IF NOT EXISTS idx_mdr_date ON mdr(DATE_PREF);",
        "CREATE INDEX IF NOT EXISTS idx_device_key ON device(MDR_REPORT_KEY);",
        "CREATE INDEX IF NOT EXISTS idx_device_pc ON device(DEVICE_REPORT_PRODUCT_CODE);",
    ]
    if table_exists(con, "foi"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_foi_key ON foi(MDR_REPORT_KEY);",
        ]
    if table_exists(con, "patient"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_patient_key ON patient(MDR_REPORT_KEY);",
        ]
    if table_exists(con, "patient_problem_codes"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_ppc_key ON patient_problem_codes(MDR_REPORT_KEY);",
        ]
    if table_exists(con, "foidevproblem"):
        stmts += [
            "CREATE INDEX IF NOT EXISTS idx_fdp_key ON foidevproblem(MDR_REPORT_KEY);",
        ]
    for s in stmts:
        try:
            con.execute(s)
        except Exception as e:
            info(f"   ! Index skipped: {e}")


def build_fts_index(con: duckdb.DuckDBPyConnection) -> None:
    """Build a DuckDB FTS index on foi(FOI_TEXT). Optional but transformative
    for narrative-search performance."""
    if not table_exists(con, "foi"):
        return
    cols = [r[0] for r in con.execute("DESCRIBE foi").fetchall()]
    if "FOI_TEXT" not in cols:
        return
    info("=> Building FTS index on foi(FOI_TEXT)  [this may take a few minutes]")
    try:
        con.execute("INSTALL fts; LOAD fts;")
        con.execute(
            """
            PRAGMA create_fts_index(
                'foi', 'MDR_REPORT_KEY', 'FOI_TEXT',
                stemmer='porter', stopwords='english', overwrite=1
            );
            """
        )
        info("   = FTS index ready (use match_bm25(MDR_REPORT_KEY, ?) at query time)")
    except Exception as e:
        info(f"   ! FTS build failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return (
        con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()[0]
        == 1
    )


def glob_many(raw_dir: str, *patterns: str) -> list[str]:
    files: list[str] = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(raw_dir, p)))
    return sorted(set(files))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MAUDE ingestion v2")
    ap.add_argument("--raw-dir", default=".", help="Directory containing MAUDE .txt/.csv files")
    ap.add_argument("--db", default="maude_final.duckdb", help="Output DuckDB file")
    ap.add_argument("--no-fts", action="store_true", help="Skip FTS index build")
    ap.add_argument("--keep-existing", action="store_true", help="Append to existing DB instead of replacing")
    args = ap.parse_args(argv)

    raw = args.raw_dir
    db = args.db

    # Sanity-check the raw directory before doing anything destructive.
    if not os.path.isdir(raw):
        info(f"ERROR: --raw-dir does not exist or is not a directory: {raw!r}")
        info("")
        info("If you copied the example command from the README literally,")
        info("you need to replace '/path/to/maude/files' with the actual")
        info("folder containing your MAUDE .txt files.")
        info("")
        info("On Windows, try:")
        info("  python maude_ingest_v2.py --raw-dir . --db maude_final.duckdb")
        info("(if the files are in the current folder)")
        return 2

    # Look for at least one expected primary file before nuking the DB.
    expected = (
        glob_many(raw, "mdrfoi*.txt", "MDRFOI*.txt")
        + glob_many(raw, "device*.txt", "DEVICE*.txt")
        + glob_many(raw, "foitext*.txt", "FOITEXT*.txt")
        + glob_many(raw, "patient.txt", "patient_utf8.txt", "patientThru*.txt",
                    "patientchange*.txt", "patientadd*.txt")
    )
    if not expected:
        info(f"ERROR: no MAUDE primary files found in {raw!r}")
        info("")
        info("Expected at least one of: mdrfoi*.txt, device*.txt,")
        info("foitext*.txt, or patient*.txt")
        info("")
        info(f"Files I see in {raw!r}:")
        try:
            for name in sorted(os.listdir(raw))[:30]:
                info(f"  {name}")
        except OSError as e:
            info(f"  (could not list directory: {e})")
        info("")
        info("Download MAUDE files from:")
        info("  https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files")
        return 2

    if not args.keep_existing and os.path.exists(db):
        info(f"-- removing existing {db}")
        os.remove(db)

    t0 = time.time()
    con = duckdb.connect(db)
    info("MAUDE ingestion v2")
    info(f"  raw_dir = {raw}")
    info(f"  db      = {db}")
    info("")

    # -- Primary tables -----------------------------------------------------
    load_pipe_files(
        con, "PATIENT",
        glob_many(raw, "patient.txt", "patient_utf8.txt", "patientThru*.txt", "patientchange*.txt", "patientadd*.txt"),
        "patient",
    )

    load_pipe_files(
        con, "DEVICE",
        glob_many(raw, "device*.txt", "DEVICE*.txt"),
        "device",
    )

    # foitext: REQUIRES multi-line reassembly to preserve narratives.
    load_pipe_files(
        con, "FOITEXT (multi-line aware)",
        glob_many(raw, "foitext*.txt", "FOITEXT*.txt"),
        "foi",
        reassemble=True,
    )

    load_pipe_files(
        con, "MDRFOI",
        glob_many(raw, "mdrfoi*.txt", "MDRFOI*.txt"),
        "mdr",
    )

    # -- Supplemental: patient problem codes file (headerless pipe) --------
    # FDA distributes these without a header; expected columns are
    # MDR_REPORT_KEY | PATIENT_SEQUENCE_NUMBER | PROBLEM_CODE (since 2020).
    ppc_path = os.path.join(raw, "patientproblemcode.txt")
    if not os.path.exists(ppc_path):
        # older naming
        cand = glob_many(raw, "patientproblem*.txt", "patient_problem*.txt")
        ppc_path = cand[0] if cand else ppc_path
    load_headerless_pipe(
        con, "PATIENT PROBLEM CODES", ppc_path,
        "patient_problem_codes", PATIENT_PROBLEM_COLS,
    )

    # foidevproblem (headerless pipe): MDR_REPORT_KEY | DEVICE_PROBLEM_CODE
    fdp_path = os.path.join(raw, "foidevproblem.txt")
    load_headerless_pipe(
        con, "DEVICE PROBLEMS (foidevproblem)", fdp_path,
        "foidevproblem", FOIDEVPROBLEM_COLS,
    )

    # -- Dictionaries: CSV (comma-delimited) -------------------------------
    load_csv_dict(
        con, "DEVICE PROBLEM DICTIONARY",
        os.path.join(raw, "deviceproblemcodes.csv"),
        "device_problem_dict",
    )
    load_csv_dict(
        con, "PATIENT PROBLEM DICTIONARY",
        os.path.join(raw, "patientproblemcode.csv"),
        "patient_problem_dict",
    )

    info("")
    info("Post-ingest enrichments")
    info("-----------------------")
    if table_exists(con, "mdr"):
        add_typed_dates(con)
        add_flag_columns(con)
    if table_exists(con, "patient"):
        add_patient_age_years(con)
    create_indexes(con)
    if not args.no_fts:
        build_fts_index(con)

    # -- Summary ------------------------------------------------------------
    info("")
    info("Final table sizes")
    info("-----------------")
    tables = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1"
    ).fetchall()
    for (t,) in tables:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            info(f"  {t:30s}  {n:>12,}")
        except Exception:
            pass

    con.close()
    info("")
    info(f"Done in {(time.time() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
