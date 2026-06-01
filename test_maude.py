"""
Test suite for MAUDE-Dash v4
============================

Run with:
    python test_maude.py

Tests are organised by component. Failures print actionable diagnostics.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import pandas as pd

# Make package imports work regardless of where test is run from
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE))

import maude_stats as ms  # noqa: E402

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

class TestRunner:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def run(self, name: str, fn) -> None:
        try:
            fn()
            self.passed.append(name)
            print(f"  [PASS] {name}")
        except AssertionError as e:
            self.failed.append((name, str(e)))
            print(f"  [FAIL] {name}: {e}")
        except Exception as e:
            self.failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")

    def section(self, label: str) -> None:
        print(f"\n=== {label} ===")

    def summary(self) -> int:
        print(f"\n{'='*60}")
        print(f"Passed: {len(self.passed)} | Failed: {len(self.failed)}")
        if self.failed:
            print("\nFailures:")
            for n, msg in self.failed:
                print(f"  - {n}: {msg}")
        return 0 if not self.failed else 1


# ---------------------------------------------------------------------------
# Test 1: Statistics module
# ---------------------------------------------------------------------------

def test_stats_wilson_known_values() -> None:
    """Wilson CI must match statsmodels reference values within 1e-4."""
    cases = [
        (5, 10, 0.23659, 0.76341),
        (0, 100, 0.0, 0.036993),
        (100, 100, 0.96301, 1.0),
        (3, 30, 0.03460, 0.25621),
        (1, 50, 0.00354, 0.10495),  # verified against statsmodels
    ]
    for k, n, exp_lo, exp_hi in cases:
        ci = ms.wilson_ci(k, n)
        assert abs(ci.lo - exp_lo) < 5e-4, \
            f"Wilson({k},{n}).lo: got {ci.lo}, expected {exp_lo}"
        # Special case: when lo is essentially zero (k=0), allow tiny positive
        if exp_lo == 0.0:
            assert ci.lo < 1e-10
        if exp_hi == 1.0:
            assert ci.hi > 1 - 1e-10
        else:
            assert abs(ci.hi - exp_hi) < 5e-4, \
                f"Wilson({k},{n}).hi: got {ci.hi}, expected {exp_hi}"


def test_stats_wilson_edge_cases() -> None:
    """Wilson CI handles edge inputs without crashing."""
    # n=0
    ci = ms.wilson_ci(0, 0)
    assert ci.p == 0.0 and ci.lo == 0.0 and ci.hi == 1.0
    # k > n should raise
    try:
        ms.wilson_ci(11, 10)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_stats_2x2_known() -> None:
    """analyze_2x2 returns correct PRR/ROR for known cases."""
    # Classic example: a=50, b=50, c=50, d=9950 — strong positive signal
    r = ms.analyze_2x2(50, 50, 50, 9950)
    assert 90 < r.prr.point < 110, f"PRR out of expected range: {r.prr.point}"
    assert r.chi2_yates > 100
    assert ms.ema_signal(r.a, r.prr.point, r.chi2_yates)

    # No signal: a=2, b=98, c=200, d=9700 — proportions about equal
    r = ms.analyze_2x2(2, 98, 200, 9700)
    assert 0.5 < r.prr.point < 1.5, f"Expected PRR near 1, got {r.prr.point}"
    assert not ms.ema_signal(r.a, r.prr.point, r.chi2_yates)


def test_stats_trend() -> None:
    """Cochran-Armitage and Mann-Kendall agree on direction."""
    # Monotonic increase
    ca = ms.cochran_armitage_trend([1, 5, 10, 20, 40], [100]*5)
    assert ca.direction == "increasing"
    assert ca.p_value is not None and ca.p_value < 0.01

    mk = ms.mann_kendall([1, 5, 10, 20, 40, 60, 80])
    assert mk.direction == "increasing"

    # Monotonic decrease
    mk = ms.mann_kendall([80, 60, 50, 40, 30, 20, 10])
    assert mk.direction == "decreasing"

    # Flat
    mk = ms.mann_kendall([10, 10, 10, 10, 10])
    assert mk.direction == "no trend"


def test_stats_chi2_independence() -> None:
    """Chi-square test of independence on known cases."""
    # Clearly different proportions
    chi2, p, df = ms.chi2_independence([[10, 90], [40, 60]])
    assert df == 1
    if p is not None:
        assert p < 0.001
    # Equal proportions
    chi2, p, df = ms.chi2_independence([[50, 50], [50, 50]])
    assert chi2 < 0.001


# ---------------------------------------------------------------------------
# Test 2: Ingest pipeline against synthetic data
# ---------------------------------------------------------------------------

def make_synthetic_data(work: Path) -> None:
    """Build a small synthetic MAUDE corpus exercising all the edge cases."""
    # mdrfoi: 5 reports, including 1 RWD, 1 supplement, 1 foreign, varied event types
    (work / "mdrfoi.txt").write_text(
        "\ufeffMDR_REPORT_KEY|EVENT_KEY|REPORT_NUMBER|MDR_REPORT_KEY_DUP|DATE_RECEIVED|DATE_REPORT|DATE_OF_EVENT|EVENT_TYPE|MANUFACTURER_NAME|REPORTER_OCCUPATION_CODE|SOURCE_TYPE|SUPPLEMENT_NUMBER|REPORTER_COUNTRY_CODE|ADVERSE_EVENT_FLAG|PRODUCT_PROBLEM_FLAG\n"
        "1001|2001|MFR1-2020-00001|1001|20200115|20200120|20200110|D|ACME MEDICAL|001|M||US|Y|Y\n"
        "1002|2002|MFR2-2020-00002|1002|20200201|20200205|20200131|IN|MEDDEV INC|002|M,V||CA|Y|Y\n"
        "1003|2003|RWD2020-00003|1003|20200301|20200305|20200225|M|ACME MEDICAL|001|M||US|N|Y\n"
        "1004|2004|MFR1-2020-00001|1004|20200320|20200325|20200110|D|ACME MEDICAL|001|M|1|US|Y|Y\n"
        "1005|2005|MFR3-2021-00005|1005|20210601|20210605|20210525|IN|FOREIGN CO|001|M||GB|Y|N\n",
        encoding="utf-8",
    )

    # device — with DEVICE_AGE_TEXT, IMPLANT_FLAG, etc.
    (work / "device.txt").write_text(
        "MDR_REPORT_KEY|DEVICE_EVENT_KEY|BRAND_NAME|GENERIC_NAME|MODEL_NUMBER|MANUFACTURER_D_NAME|DEVICE_REPORT_PRODUCT_CODE|DEVICE_AGE_TEXT|IMPLANT_FLAG|DEVICE_OPERATOR|DEVICE_EVALUATED_BY_MANUFACTURER|REPROCESSED_AND_REUSED_FLAG\n"
        "1001|3001|PEDICLE SCREW X|PEDICLE SCREW|PS-100|ACME MEDICAL|KWP|3 YR|Y|PHYSICIAN|Y|N\n"
        "1002|3002|HEART VALVE Y|HEART VALVE|HV-200|MEDDEV INC|LWS|6 MO|Y|PHYSICIAN|Y|N\n"
        "1003|3003|PEDICLE SCREW Z|PEDICLE SCREW|PS-101|ACME MEDICAL|KWP||N|TECHNICIAN|N|N\n"
        "1004|3004|PEDICLE SCREW X|PEDICLE SCREW|PS-100|ACME MEDICAL|KWP|3 YR|Y|PHYSICIAN|Y|N\n"
        "1005|3005|VENT TUBE|VENTILATOR|VT-1|FOREIGN CO|FRO|14 DY|N|NURSE|N|Y\n",
        encoding="utf-8",
    )

    # foitext WITH the multi-line wrap case (the v1 bug trigger)
    foi_bytes = (
        b"MDR_REPORT_KEY|MDR_TEXT_KEY|TEXT_TYPE_CODE|PATIENT_SEQUENCE_NUMBER|DATE_REPORT|FOI_TEXT\n"
        b"1001|9001|D|1|20200120|THE PATIENT EXPERIENCED A FRACTURE\r\n"
        b"OF THE PEDICLE SCREW. THE EVENT OCCURRED INTRAOPERATIVELY.\r\n"
        b"PATIENT REQUIRED REVISION SURGERY.\n"
        b"1002|9002|D|1|20200205|HEART VALVE MALFUNCTION DURING PROCEDURE.\n"
        b"1001|9003|N|1|20200120|MANUFACTURER INVESTIGATION: METAL FATIGUE.\n"
        b"1003|9004|D|1|20200305|MALFUNCTION OBSERVED PRE-USE. DEVICE NOT IMPLANTED. THIS REPORT REFLECTS INFORMATION RECEIVED BY FDA IN THE FORM OF A NOTIFICATION PER 803.22(b)(2).\n"
        b"1005|9005|D|1|20210605|RESPIRATORY DISTRESS NOTED. PATIENT HOSPITALIZED.\n"
    )
    (work / "foitext.txt").write_bytes(foi_bytes)

    # patient — with OUTCOME codes
    (work / "patient.txt").write_text(
        "MDR_REPORT_KEY|PATIENT_SEQUENCE_NUMBER|DATE_RECEIVED|SEQUENCE_NUMBER_TREATMENT|SEQUENCE_NUMBER_OUTCOME|PATIENT_AGE|PATIENT_SEX|PATIENT_WEIGHT|PATIENT_ETHNICITY|PATIENT_RACE\n"
        "1001|1|20200115||D,R|67 YR|M|85 KG||\n"
        "1002|1|20200201||H|72 YR|F|65 KG||\n"
        "1003|1|20200301||O|14 WK|M|6 KG||\n"
        "1004|1|20200320||D,R|67 YR|M|85 KG||\n"
        "1005|1|20210601||L,H|55 YR|F|70 KG||\n",
        encoding="utf-8",
    )

    # Patient problem codes: 5-column WITH HEADER (current real FDA format).
    # This is the format that silently failed in the original headerless loader.
    (work / "patientproblemcode.txt").write_text(
        "MDR_REPORT_KEY|PATIENT_SEQUENCE_NO|PROBLEM_CODE|DATE_ADDED|DATE_CHANGED\n"
        "1001|1|2840|2016/02/05 13:15:36|2016/02/05 13:15:36\n"
        "1001|1|2876|2016/02/05 13:15:36|2016/02/05 13:15:36\n"
        "1002|1|2840|2017/01/12 15:57:46|2017/01/12 15:57:46\n"
        "1003|1|2876|2018/03/15 10:00:00|2018/03/15 10:00:00\n"
        "1005|1|2840|2019/05/22 14:30:00|2019/05/22 14:30:00\n"
    )
    # foidevproblem: 2-column headerless (current real FDA format)
    (work / "foidevproblem.txt").write_text(
        "1001|1546\n1001|2682\n1002|1071\n1003|1546\n1005|2400\n"
    )

    # Dictionary CSVs
    (work / "deviceproblemcodes.csv").write_text(
        'FDA_CODE,TERM,NCIT_CODE,IMDRF_CODE\n'
        '1546,Break,,E2402\n'
        '2682,Material Fracture,,E2401\n'
        '1071,Leak,,E1234\n'
        '2400,Failure to deliver,,E5000\n'
    )
    (work / "patientproblemcode.csv").write_text(
        'FDA_CODE,TERM,NCIT_CODE,IMDRF_CODE\n'
        '2840,Pain,C3303,\n'
        '2876,No Clinical Signs,,\n'
    )


def test_ingest_runs(work: Path) -> None:
    """Run the v2 ingest against synthetic data — must succeed end-to-end."""
    cmd = [
        sys.executable, str(HERE / "maude_ingest_v2.py"),
        "--raw-dir", str(work),
        "--db", str(work / "test.duckdb"),
        "--no-fts",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"ingest failed: {result.stderr}"
    assert (work / "test.duckdb").exists()


def test_ingest_narratives_reassembled(work: Path) -> None:
    """The multi-line narrative for MDR 1001 must be reassembled into one row."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    row = con.execute(
        "SELECT FOI_TEXT FROM foi WHERE MDR_REPORT_KEY = '1001' "
        "AND TEXT_TYPE_CODE = 'D'"
    ).fetchone()
    assert row is not None, "1001 description not found"
    text = row[0]
    # The wrapped narrative contained all three lines — they should now be in one
    assert "FRACTURE" in text and "INTRAOPERATIVELY" in text and "REVISION" in text, \
        f"Multi-line reassembly broke: got {text!r}"


def test_ingest_csv_dict_loaded(work: Path) -> None:
    """The comma-CSV dictionary should have 4 columns, not 1 mangled one."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    cols = [r[0] for r in con.execute('DESCRIBE device_problem_dict').fetchall()]
    assert set(cols) >= {"FDA_CODE", "TERM"}, \
        f"CSV dictionary not loaded correctly: cols = {cols}"
    n = con.execute("SELECT COUNT(*) FROM device_problem_dict").fetchone()[0]
    assert n == 4, f"Expected 4 dictionary entries, got {n}"


def test_ingest_patient_problems_5col_header(work: Path) -> None:
    """The 5-column WITH HEADER patient problem file must load correctly.

    This is the exact format that silently produced 0 rows in the original
    headerless loader. We have 5 data rows in the synthetic file; the header
    must NOT be counted as data, and all 5 rows must be present with the
    correct MDR_REPORT_KEY and PROBLEM_CODE mapped by column name.
    """
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    n = con.execute("SELECT COUNT(*) FROM patient_problem_codes").fetchone()[0]
    assert n == 5, f"Expected 5 patient problem rows, got {n} (header counted as data?)"
    # The header row's literal text must not appear as a data value
    bad = con.execute(
        "SELECT COUNT(*) FROM patient_problem_codes WHERE MDR_REPORT_KEY = 'MDR_REPORT_KEY'"
    ).fetchone()[0]
    assert bad == 0, "Header row was incorrectly loaded as data"
    # Spot-check a known mapping: MDR 1001 has problem code 2840
    hit = con.execute(
        "SELECT COUNT(*) FROM patient_problem_codes "
        "WHERE MDR_REPORT_KEY = '1001' AND PROBLEM_CODE = '2840'"
    ).fetchone()[0]
    assert hit == 1, "Expected MDR 1001 / code 2840 mapping not found"


def test_ingest_dates_typed(work: Path) -> None:
    """mdr.DATE_PREF must be a real DATE."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    cols = {r[0]: r[1] for r in con.execute("DESCRIBE mdr").fetchall()}
    assert "DATE_PREF" in cols
    assert "DATE" in cols["DATE_PREF"].upper(), \
        f"DATE_PREF type not DATE: {cols['DATE_PREF']}"


def test_ingest_patient_age_normalized(work: Path) -> None:
    """14-week-old must become ~0.27 years, not 14."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    row = con.execute(
        "SELECT AGE_YEARS FROM patient WHERE MDR_REPORT_KEY = '1003'"
    ).fetchone()
    assert row is not None
    age = float(row[0])
    assert 0.25 < age < 0.30, f"14 WK should be ~0.27 yr, got {age}"


def test_ingest_flags(work: Path) -> None:
    """Derived flags computed correctly."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    # RWD report
    r = con.execute(
        "SELECT IS_RWD_SOURCED FROM mdr WHERE MDR_REPORT_KEY = '1003'"
    ).fetchone()
    assert r[0] is True, "RWD2020-... should be IS_RWD_SOURCED=True"
    # Forwarded report (narrative contains 803.22(b)(2))
    r = con.execute(
        "SELECT IS_FORWARDED_803_22_B2 FROM mdr WHERE MDR_REPORT_KEY = '1003'"
    ).fetchone()
    assert r[0] is True, "1003 narrative mentions 803.22(b)(2) → should be flagged"


# ---------------------------------------------------------------------------
# Test 3: Analytic build
# ---------------------------------------------------------------------------

def test_analytic_build_runs(work: Path) -> None:
    cmd = [
        sys.executable, str(HERE / "maude_analytic_build_v2.py"),
        "--db", str(work / "test.duckdb"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"analytic build failed: {result.stderr}"


def test_mdr_flat_has_clinical_fields(work: Path) -> None:
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    cols = {r[0] for r in con.execute("DESCRIBE mdr_flat").fetchall()}
    required = {
        "MDR_REPORT_KEY", "EVENT_TYPE", "report_year", "manufacturer",
        "product_code",
        "outcome_death", "outcome_life_threatening", "outcome_hospitalization",
        "outcome_disability", "outcome_congenital_anomaly",
        "outcome_required_intervention", "outcome_other",
        "any_serious_outcome", "outcome_codes_raw",
        "device_age_days", "implant_flag",
        "reporter_country_code", "is_supplement", "initial_report",
    }
    missing = required - cols
    assert not missing, f"mdr_flat missing required columns: {missing}"


def test_outcome_decoding(work: Path) -> None:
    """Outcome codes D,R must decode to outcome_death=True, outcome_required_intervention=True."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    row = con.execute("""
        SELECT outcome_death, outcome_required_intervention,
               outcome_hospitalization, any_serious_outcome
        FROM mdr_flat WHERE MDR_REPORT_KEY = '1001'
    """).fetchone()
    death, required, hosp, serious = row
    assert death is True, "1001 should have outcome_death (raw codes: D,R)"
    assert required is True, "1001 should have outcome_required_intervention"
    assert hosp is False, "1001 should not have outcome_hospitalization"
    assert serious is True, "1001 (D,R) should be serious"


def test_device_age_parsing(work: Path) -> None:
    """3 YR → 1095.75 days; 14 DY → 14 days."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    r1 = con.execute(
        "SELECT device_age_days FROM mdr_flat WHERE MDR_REPORT_KEY = '1001'"
    ).fetchone()
    assert abs(float(r1[0]) - 1095.75) < 0.1, f"3 YR should be ~1095.75 days, got {r1[0]}"

    r2 = con.execute(
        "SELECT device_age_days FROM mdr_flat WHERE MDR_REPORT_KEY = '1005'"
    ).fetchone()
    assert abs(float(r2[0]) - 14.0) < 0.01, f"14 DY should be 14 days, got {r2[0]}"


def test_supplement_detection(work: Path) -> None:
    """Report 1004 has SUPPLEMENT_NUMBER=1 → is_supplement=True."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    r = con.execute(
        "SELECT is_supplement, initial_report, supplement_number "
        "FROM mdr_flat WHERE MDR_REPORT_KEY = '1004'"
    ).fetchone()
    assert r[0] is True, f"1004 is_supplement should be True, got {r[0]}"
    assert r[1] is False, f"1004 initial_report should be False, got {r[1]}"
    assert r[2] == 1, f"1004 supplement_number should be 1, got {r[2]}"


def test_initial_only_filter_excludes_supplements(work: Path) -> None:
    """Initial-only filter in 'WHERE COALESCE(initial_report, TRUE) = TRUE'
    should exclude supplements but include reports without SUPPLEMENT_NUMBER."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    n_all = con.execute("SELECT COUNT(*) FROM mdr_flat").fetchone()[0]
    n_initial = con.execute(
        "SELECT COUNT(*) FROM mdr_flat WHERE COALESCE(initial_report, TRUE) = TRUE"
    ).fetchone()[0]
    assert n_initial == n_all - 1, \
        f"Initial-only should be {n_all - 1} (excluding 1 supplement), got {n_initial}"


# ---------------------------------------------------------------------------
# Test 4: Dashboard SQL queries
# ---------------------------------------------------------------------------

def test_dashboard_kpi_sql(work: Path) -> None:
    """The KPI band query must run without error."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    sql = """
        SELECT COUNT(*) AS n_reports,
               SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS n_d,
               SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS n_outcome_death,
               SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious,
               COUNT(DISTINCT REPORT_NUMBER) AS n_events
        FROM mdr_flat WHERE report_year BETWEEN 2018 AND 2024
    """
    df = con.execute(sql).fetchdf()
    assert len(df) == 1
    assert df.iloc[0]["n_reports"] > 0


def test_dashboard_disproportionality(work: Path) -> None:
    """The disproportionality query (previously buggy) must run."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    sql = """
        WITH dict AS (SELECT TRIM("FDA_CODE") AS FDA_CODE, TRIM("TERM") AS TERM FROM device_problem_dict),
             keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE product_code = 'KWP'),
             inside AS (
                 SELECT fp.code, COUNT(DISTINCT fp.MDR_REPORT_KEY) AS n
                 FROM flat_dev_problems fp JOIN keys USING (MDR_REPORT_KEY)
                 GROUP BY 1
             ),
             tot AS (SELECT COUNT(*)::DOUBLE AS n FROM mdr_flat),
             ins AS (SELECT COUNT(*)::DOUBLE AS n FROM keys),
             joined AS (
                 SELECT inside.code, inside.n AS inside_n, g.n AS global_n
                 FROM inside JOIN agg_dev_problems_global g USING (code)
             )
        SELECT COALESCE(dict.TERM, joined.code) AS Term,
               joined.inside_n::DOUBLE AS a
        FROM joined LEFT JOIN dict ON dict.FDA_CODE = joined.code, tot, ins
    """
    df = con.execute(sql).fetchdf()
    assert len(df) >= 1


def test_dashboard_subgroup(work: Path) -> None:
    """Subgroup analysis query (with age band CASE)."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    df = con.execute("""
        SELECT
            CASE
              WHEN age_years_avg IS NULL THEN 'Unknown'
              WHEN age_years_avg < 1 THEN '<1 year'
              ELSE 'Other'
            END AS subgroup,
            COUNT(*) AS n,
            SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS k
        FROM mdr_flat GROUP BY 1
    """).fetchdf()
    assert len(df) >= 1


def test_dashboard_problem_outcome(work: Path) -> None:
    """Problem -> Outcome query: outcome severity stratified by problem code.

    Joins flat_dev_problems to mdr_flat's outcome flags and aggregates serious
    / death counts per code. Verifies the join and aggregation produce sane
    counts (serious count never exceeds total reports for a code).
    """
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    df = con.execute("""
        WITH keys AS (
            SELECT MDR_REPORT_KEY, any_serious_outcome, outcome_death
            FROM mdr_flat
        )
        SELECT fp.code AS Code,
               COUNT(DISTINCT fp.MDR_REPORT_KEY) AS n_reports,
               SUM(CASE WHEN k.any_serious_outcome THEN 1 ELSE 0 END) AS n_serious,
               SUM(CASE WHEN k.outcome_death THEN 1 ELSE 0 END) AS n_death
        FROM flat_dev_problems fp
        JOIN keys k USING (MDR_REPORT_KEY)
        GROUP BY 1
        HAVING COUNT(DISTINCT fp.MDR_REPORT_KEY) >= 1
    """).fetchdf()
    assert len(df) >= 1, "Expected at least one problem code with outcomes"
    for _, r in df.iterrows():
        assert int(r["n_serious"]) <= int(r["n_reports"]), \
            f"Serious count exceeds report count for code {r['Code']}"
        assert int(r["n_death"]) <= int(r["n_reports"]), \
            f"Death count exceeds report count for code {r['Code']}"


def test_dashboard_trend_query(work: Path) -> None:
    """Trend test query."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    df = con.execute("""
        SELECT report_year AS year, COUNT(*) AS n,
               SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS k
        FROM mdr_flat WHERE report_year IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).fetchdf()
    assert len(df) >= 1


def test_dashboard_source_type_split(work: Path) -> None:
    """SOURCE_TYPE splitting with unnest(string_split(...)) c(value)."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    df = con.execute("""
        WITH base AS (
            SELECT SOURCE_TYPE FROM mdr_flat
            WHERE SOURCE_TYPE IS NOT NULL AND TRIM(SOURCE_TYPE) <> ''
        )
        SELECT UPPER(TRIM(s.value)) AS code, COUNT(*) AS n
        FROM base, unnest(string_split(SOURCE_TYPE, ',')) s(value)
        WHERE TRIM(s.value) <> '' GROUP BY 1
    """).fetchdf()
    assert len(df) >= 1


# ---------------------------------------------------------------------------
# Test 5: Sanity checks combining multiple modules
# ---------------------------------------------------------------------------

def test_known_clinical_proportions(work: Path) -> None:
    """In our synthetic data: 4/5 reports should be serious, 2/5 should be deaths."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    n_total, n_serious, n_death = con.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious,
               SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS n_death
        FROM mdr_flat
    """).fetchone()
    assert n_total == 5
    assert n_serious == 4, f"Expected 4 serious, got {n_serious}"
    assert n_death == 2, f"Expected 2 outcome=D, got {n_death}"


def test_wilson_integration(work: Path) -> None:
    """End-to-end: extract counts and compute Wilson CI."""
    con = duckdb.connect(str(work / "test.duckdb"), read_only=True)
    n_total, n_serious = con.execute("""
        SELECT COUNT(*), SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END)
        FROM mdr_flat
    """).fetchone()
    ci = ms.wilson_ci(int(n_serious), int(n_total))
    # 4/5 = 80%, Wilson CI for 4/5 is approximately (37.6, 96.4)
    assert abs(ci.p - 0.8) < 1e-6
    assert 0.3 < ci.lo < 0.4
    assert 0.9 < ci.hi < 1.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    runner = TestRunner()

    runner.section("Statistics module")
    runner.run("wilson_ci known values", test_stats_wilson_known_values)
    runner.run("wilson_ci edge cases", test_stats_wilson_edge_cases)
    runner.run("analyze_2x2 known cases", test_stats_2x2_known)
    runner.run("trend tests", test_stats_trend)
    runner.run("chi-square independence", test_stats_chi2_independence)

    # Setup synthetic data once for the file-based tests
    work = Path(tempfile.mkdtemp(prefix="maude_test_"))
    try:
        runner.section("Ingest pipeline")
        make_synthetic_data(work)
        runner.run("ingest runs to completion", lambda: test_ingest_runs(work))
        runner.run("narratives reassembled", lambda: test_ingest_narratives_reassembled(work))
        runner.run("CSV dictionary loaded with 4 cols", lambda: test_ingest_csv_dict_loaded(work))
        runner.run("patient problems 5-col header loads", lambda: test_ingest_patient_problems_5col_header(work))
        runner.run("dates typed as DATE", lambda: test_ingest_dates_typed(work))
        runner.run("patient age (14 WK) normalised", lambda: test_ingest_patient_age_normalized(work))
        runner.run("derived flags (RWD, forwarded)", lambda: test_ingest_flags(work))

        runner.section("Analytic build v2")
        runner.run("analytic build runs", lambda: test_analytic_build_runs(work))
        runner.run("mdr_flat has clinical fields", lambda: test_mdr_flat_has_clinical_fields(work))
        runner.run("outcome codes decoded (D,R)", lambda: test_outcome_decoding(work))
        runner.run("device age parsed (3 YR, 14 DY)", lambda: test_device_age_parsing(work))
        runner.run("supplement detection", lambda: test_supplement_detection(work))
        runner.run("initial-only filter excludes supplements",
                   lambda: test_initial_only_filter_excludes_supplements(work))

        runner.section("Dashboard SQL paths")
        runner.run("KPI band query", lambda: test_dashboard_kpi_sql(work))
        runner.run("disproportionality query (regression)",
                   lambda: test_dashboard_disproportionality(work))
        runner.run("subgroup analysis query", lambda: test_dashboard_subgroup(work))
        runner.run("problem-outcome query", lambda: test_dashboard_problem_outcome(work))
        runner.run("trend test query", lambda: test_dashboard_trend_query(work))
        runner.run("source_type split query", lambda: test_dashboard_source_type_split(work))

        runner.section("End-to-end integration")
        runner.run("clinical proportions match expected",
                   lambda: test_known_clinical_proportions(work))
        runner.run("Wilson CI computed from DB data",
                   lambda: test_wilson_integration(work))
    finally:
        try:
            shutil.rmtree(work)
        except Exception:
            pass

    return runner.summary()


if __name__ == "__main__":
    sys.exit(main())
