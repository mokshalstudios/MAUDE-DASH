# maude_dashboard_final_v10_1.py
# MAUDE DASH
# Mokshal Porwal, Allegheny Health Network
#
# v10.1 (final, optimized):
# - All tabs preserved:
#   📄 Report Preview, 📈 Yearly Trends, ⚡ Event Trends, 🧑 Demographics,
#   🧑‍⚕️ Reporter Analysis, 💥 Problem Codes, ⏳ Time-to-Report,
#   📝 Narrative Analysis, 📜 Raw Narratives, 📦 Master Export
# - Product-code lookup filter
# - Binder-safe dictionary joins (supports merged dict column)
# - KPI metrics null-safe (no pd.NA ambiguity)
# - Narrative word cloud & table in sync; table includes single clinical terms + collocations
# - Performance: prefilter MDR keys (LIMIT 5000) and reuse across all tabs to prevent long scans

import os
from io import BytesIO
from datetime import datetime
from typing import Tuple, Optional, List, Dict
import re
from collections import Counter

import duckdb
import pandas as pd
import streamlit as st
import plotly.express as px
from wordcloud import WordCloud

# Optional NLTK collocations (safe fallback if unavailable)
_NLTK_OK = True
try:
    from nltk.collocations import BigramCollocationFinder, TrigramCollocationFinder
except Exception:
    _NLTK_OK = False
    BigramCollocationFinder = TrigramCollocationFinder = None

# --------------------
# Config
# --------------------
PREFETCH_LIMIT = 5000  # number of MDRs to prefilter for speed

# --------------------
# Page setup (title + subheader)
# --------------------
st.set_page_config(page_title="MAUDE Advanced Research Dashboard", layout="wide")
st.title("MAUDE Advanced Research Dashboard")
st.caption("**Mokshal Porwal, Allegheny Health Network**")

# --------------------
# Utilities
# --------------------
@st.cache_data
def duck_read(_db_path: str, sql: str, params: Tuple = ()) -> pd.DataFrame:
    with duckdb.connect(_db_path, read_only=True) as con:
        return con.execute(sql, params).fetchdf()

def validate_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()

def df_to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buf = BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name=sheet_name)
    except Exception:
        try:
            with pd.ExcelWriter(buf, engine="xlsxwriter") as w:
                df.to_excel(w, index=False, sheet_name=sheet_name)
        except Exception:
            buf = BytesIO()
            buf.write(df.to_csv(index=False).encode("utf-8"))
            return buf.getvalue()
    return buf.getvalue()

def get_columns(con, table: str) -> List[str]:
    try:
        return [r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()]
    except Exception:
        return []

def detect_problem_dict_mode(con, table: str) -> Dict[str, str]:
    """Detect whether dict table has separate columns or a merged header including commas."""
    try:
        cols = get_columns(con, table)
    except Exception:
        return {"mode": "missing"}
    norm = {c.upper(): c for c in cols}
    if "FDA_CODE" in norm and "TERM" in norm:
        return {"mode": "separate", "code_col": norm["FDA_CODE"], "term_col": norm["TERM"]}
    merged = [c for c in cols if "FDA_CODE" in c and "TERM" in c and "," in c]
    if merged:
        return {"mode": "merged", "merged_col": merged[0]}
    return {"mode": "missing"}

def quote_ident(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'

def dict_cte_sql(mode: Dict[str, str], table: str) -> str:
    """Return a CTE SELECT that normalizes a dict table to (FDA_CODE, TERM)."""
    tbl = quote_ident(table)
    m = mode.get("mode")
    if m == "separate":
        return f"SELECT TRIM({quote_ident(mode['code_col'])}) AS FDA_CODE, TRIM({quote_ident(mode['term_col'])}) AS TERM FROM {tbl}"
    if m == "merged":
        merged = quote_ident(mode["merged_col"])
        return f"""SELECT
            TRIM(list_extract(str_split({merged}, ','), 1)) AS FDA_CODE,
            TRIM(list_extract(str_split({merged}, ','), 2)) AS TERM
        FROM {tbl}"""
    return "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"

def build_where_and_params(mdr_key_filter: str, device_terms_raw: str, manufacturer_filter: str,
                           product_code_filter: str, narrative_filter: str,
                           date_from: str, date_to: str, FOI_TEXT_COL: Optional[str]):
    # These expressions are referenced in multiple queries
    date_pref_sql = """COALESCE(try_strptime(m.DATE_RECEIVED, '%Y%m%d'),
                                 try_strptime(m.REPORT_DATE, '%Y%m%d'),
                                 try_strptime(m.DATE_RECEIVED, '%m/%d/%Y'),
                                 try_strptime(m.REPORT_DATE, '%m/%d/%Y'))"""
    event_date_sql = """COALESCE(try_strptime(m.DATE_OF_EVENT, '%Y%m%d'),
                                  try_strptime(m.DATE_OF_EVENT, '%m/%d/%Y'))"""
    if mdr_key_filter.strip():
        return "m.MDR_REPORT_KEY = ?", [mdr_key_filter.strip()], date_pref_sql, event_date_sql

    where_parts, params = [], []
    where_parts.append(f"{date_pref_sql} BETWEEN try_strptime(?, '%Y%m%d') AND try_strptime(?, '%Y%m%d')")
    params.extend([date_from, date_to])

    if manufacturer_filter.strip():
        mf = f"%{manufacturer_filter.strip().lower()}%"
        where_parts.append("(LOWER(COALESCE(m.MANUFACTURER_NAME,'')) LIKE ? OR LOWER(COALESCE(d.MANUFACTURER_D_NAME,'')) LIKE ?)")
        params.extend([mf, mf])

    if product_code_filter.strip():
        pc = f"%{product_code_filter.strip().lower()}%"
        where_parts.append("LOWER(COALESCE(d.DEVICE_REPORT_PRODUCT_CODE,'')) LIKE ?")
        params.append(pc)

    if narrative_filter.strip() and FOI_TEXT_COL:
        nf = f"%{narrative_filter.strip().lower()}%"
        where_parts.append(f"m.MDR_REPORT_KEY IN (SELECT MDR_REPORT_KEY FROM foi WHERE LOWER({FOI_TEXT_COL}) LIKE ?)")
        params.append(nf)

    terms = [t.strip() for t in device_terms_raw.split(';') if t.strip()]
    if terms:
        term_blocks = ["(LOWER(BRAND_NAME) LIKE ? OR LOWER(GENERIC_NAME) LIKE ? OR LOWER(MODEL_NUMBER) LIKE ?)" for _ in terms]
        where_parts.append(f"m.MDR_REPORT_KEY IN (SELECT MDR_REPORT_KEY FROM device WHERE {' OR '.join(term_blocks)})")
        for t in terms:
            patt = f"%{t.lower()}%"
            params.extend([patt, patt, patt])

    return " AND ".join(where_parts), params, date_pref_sql, event_date_sql

# --------------------
# Sidebar
# --------------------
st.sidebar.header("Filters")
db_path = st.sidebar.text_input("Path to DuckDB", value="maude_final.duckdb")
mdr_key_filter = st.sidebar.text_input("Direct lookup by MDR Report Key", help="Overrides other filters if provided.")
st.sidebar.markdown("---")

device_terms_raw = st.sidebar.text_input("Device name contains (brand/generic/model; semicolon-separated)")
manufacturer_filter = st.sidebar.text_input("Manufacturer contains (optional)")
product_code_filter = st.sidebar.text_input("Device product code contains (optional)")
date_from = st.sidebar.text_input("Date from (YYYYMMDD)", value="20150101")
date_to = st.sidebar.text_input("Date to (YYYYMMDD)", value="20241231")
narrative_filter = st.sidebar.text_input("Narrative contains (optional)")
max_rows = st.sidebar.number_input("Max preview/export rows", min_value=100, max_value=100000, value=1000, step=100)

st.sidebar.markdown("---")
st.sidebar.header("📊 Download All Data Tabs")
st.sidebar.caption("Buttons appear after you visit a tab once.")
export_container = st.sidebar.container()

st.sidebar.markdown("---")
extra_stop = st.sidebar.text_input("Additional narrative stopwords (comma-separated)", value="")
min_ngram_count = st.sidebar.slider("Min n-gram count (Narrative/Terms)", min_value=1, max_value=50, value=10)
clinical_focus = st.sidebar.checkbox("Clinical-only heuristic (narrative)", value=True)

# --------------------
# DB validation
# --------------------
if not os.path.exists(db_path):
    st.error(f"Database not found: {db_path}")
    st.stop()

with duckdb.connect(db_path, read_only=True) as con:
    def check_table(t):
        return con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name ILIKE ?", [t]).fetchone()[0] == 1

    if not (check_table("mdr") and check_table("device")):
        st.error("Missing required tables 'mdr' or 'device'.")
        st.stop()

    FOI_TEXT_COL = 'FOI_TEXT' if check_table("foi") else None
    HAS_PATIENT = check_table("patient")
    HAS_PATPROB = check_table("patient_problem_codes")
    HAS_FOIDEV = check_table("foidevproblem")
    HAS_DEV_DICT = check_table("device_problem_dict")
    HAS_PAT_DICT = check_table("patient_problem_dict")

    dev_dict_mode = detect_problem_dict_mode(con, "device_problem_dict") if HAS_DEV_DICT else {"mode": "missing"}
    pat_dict_mode = detect_problem_dict_mode(con, "patient_problem_dict") if HAS_PAT_DICT else {"mode": "missing"}

# Require at least one criterion (unless MDR key)
if not any([mdr_key_filter.strip(), device_terms_raw.strip(), manufacturer_filter.strip(), product_code_filter.strip()]):
    st.warning("Enter an MDR Report Key, Device Term, Manufacturer, or Product Code to begin.")
    st.stop()

if not (validate_yyyymmdd(date_from) and validate_yyyymmdd(date_to)) and not mdr_key_filter.strip():
    st.error("Dates must be in YYYYMMDD format.")
    st.stop()

# WHERE clause strings
where_sql, params, date_pref_sql, event_date_sql = build_where_and_params(
    mdr_key_filter, device_terms_raw, manufacturer_filter, product_code_filter,
    narrative_filter, date_from, date_to, FOI_TEXT_COL
)

# --------------------
# KPI metrics (NULL-safe to avoid pd.NA bool issues)
# --------------------
metrics_sql = f"""
WITH base_keys AS (
  SELECT DISTINCT m.MDR_REPORT_KEY, m.EVENT_TYPE, {event_date_sql} as evt, {date_pref_sql} as rcv
  FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
  WHERE {where_sql}
  LIMIT {PREFETCH_LIMIT}
)
SELECT COUNT(*) AS total,
       SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS d,
       SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS i,
       SUM(CASE WHEN EVENT_TYPE='M' THEN 1 ELSE 0 END) AS m,
       approx_quantile(date_diff('day', evt, rcv), 0.5) FILTER (WHERE evt IS NOT NULL AND rcv IS NOT NULL AND date_diff('day', evt, rcv) BETWEEN 0 AND 365) AS med_lag,
       approx_quantile(date_diff('day', evt, rcv), 0.9) FILTER (WHERE evt IS NOT NULL AND rcv IS NOT NULL AND date_diff('day', evt, rcv) BETWEEN 0 AND 365) AS p90_lag,
       avg(date_diff('day', evt, rcv)) FILTER (WHERE evt IS NOT NULL AND rcv IS NOT NULL AND date_diff('day', evt, rcv) BETWEEN 0 AND 365) AS mean_lag
FROM base_keys;
"""
m_df = duck_read(db_path, metrics_sql, tuple(params))
if not m_df.empty:
    m = m_df.iloc[0]
    def nz(v, cast=float):
        return cast(v) if pd.notna(v) else cast(0)
    total_val = nz(m['total'], int)
    d_val     = nz(m['d'], int)
    i_val     = nz(m['i'], int)
    mal_val   = nz(m['m'], int)
    med_lag   = nz(m['med_lag'], float)
    p90_lag   = nz(m['p90_lag'], float)
    mean_lag  = nz(m['mean_lag'], float)

    c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
    c1.metric("Total Reports", f"{total_val:,}")
    c2.metric("Deaths", f"{d_val:,}")
    c3.metric("Injuries", f"{i_val:,}")
    c4.metric("Malfunctions", f"{mal_val:,}")
    c5.metric("Median Lag", f"{med_lag:.1f} d")
    c6.metric("90th % Lag", f"{p90_lag:.1f} d")
    c7.metric("Mean Lag", f"{mean_lag:.1f} d")

# Container for per-tab exports
exports: Dict[str, pd.DataFrame] = {}

# --------------------
# Tabs (ALL preserved)
# --------------------
tab_list = [
    "📄 Report Preview", "📈 Yearly Trends", "⚡ Event Trends", "🧑 Demographics",
    "🧑‍⚕️ Reporter Analysis", "💥 Problem Codes", "⏳ Time-to-Report",
    "📝 Narrative Analysis", "📜 Raw Narratives", "📦 Master Export"
]
(preview_tab, yearly_trends_tab, event_trends_tab, demographics_tab,
 reporter_tab, problems_tab, lag_tab, narrative_tab,
 raw_tab, master_tab) = st.tabs(tab_list)

# --------------------
# Report Preview (filter-first)
# --------------------
with preview_tab:
    st.subheader("Preview of Matching Reports")

    preview_sql = f"""
    WITH base_keys AS (
        SELECT DISTINCT m.MDR_REPORT_KEY, m.REPORT_NUMBER,
               {date_pref_sql} AS d, m.EVENT_TYPE, m.MANUFACTURER_NAME
        FROM mdr m
        LEFT JOIN device d ON d.MDR_REPORT_KEY = m.MDR_REPORT_KEY
        WHERE {where_sql}
        LIMIT {PREFETCH_LIMIT}
    ),
    dev_first AS (
        SELECT d.*, row_number() OVER (PARTITION BY d.MDR_REPORT_KEY ORDER BY d.DEVICE_EVENT_KEY) AS rn
        FROM device d
        JOIN base_keys b ON d.MDR_REPORT_KEY = b.MDR_REPORT_KEY
    ),
    dev1 AS (SELECT * FROM dev_first WHERE rn = 1)
    SELECT b.MDR_REPORT_KEY, b.REPORT_NUMBER, b.d AS DATE_PREF,
           b.EVENT_TYPE,
           COALESCE(dev1.MANUFACTURER_D_NAME, b.MANUFACTURER_NAME) AS MANUFACTURER,
           dev1.BRAND_NAME, dev1.GENERIC_NAME, dev1.MODEL_NUMBER, dev1.DEVICE_REPORT_PRODUCT_CODE
    FROM base_keys b
    LEFT JOIN dev1 ON dev1.MDR_REPORT_KEY = b.MDR_REPORT_KEY
    ORDER BY b.d DESC NULLS LAST, b.MDR_REPORT_KEY
    LIMIT {int(max_rows)};
    """
    df_prev = duck_read(db_path, preview_sql, tuple(params))
    if not df_prev.empty:
        st.dataframe(df_prev, use_container_width=True, height=420)
        exports["report_preview"] = df_prev
    else:
        st.info("No matching reports found.")

# --------------------
# Yearly Trends (filter-first)
# --------------------
with yearly_trends_tab:
    st.subheader("Yearly Reports by Device Term")
    terms = [t.strip() for t in device_terms_raw.split(';') if t.strip()]
    if terms and not mdr_key_filter.strip():
        year_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY, {date_pref_sql} AS dt
            FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        )
        SELECT strftime('%Y', dt) AS year, COUNT(DISTINCT MDR_REPORT_KEY) as count
        FROM base_keys WHERE dt IS NOT NULL
        GROUP BY year ORDER BY year;
        """
        df_year = duck_read(db_path, year_sql, tuple(params))
        if not df_year.empty:
            fig = px.bar(df_year, x="year", y="count", title="Reports per Year", height=450)
            st.plotly_chart(fig, use_container_width=True)
            exports["yearly_trends"] = df_year
        else:
            st.info("No data available for yearly trends with these filters.")
    elif mdr_key_filter.strip():
        st.info("Yearly trend not applicable for a single MDR key lookup.")
    else:
        st.info("Enter device terms to see yearly counts.")

# --------------------
# Event Trends (filter-first)
# --------------------
with event_trends_tab:
    st.subheader("Yearly Trends by Event Type")
    et_sql = f"""
    WITH base_keys AS (
        SELECT DISTINCT m.MDR_REPORT_KEY, {date_pref_sql} AS dt, m.EVENT_TYPE
        FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
        WHERE {where_sql}
        LIMIT {PREFETCH_LIMIT}
    )
    SELECT strftime('%Y', dt) AS year, EVENT_TYPE, COUNT(DISTINCT MDR_REPORT_KEY) as count
    FROM base_keys WHERE dt IS NOT NULL AND EVENT_TYPE IN ('D','IN','M')
    GROUP BY year, EVENT_TYPE ORDER BY year, EVENT_TYPE;
    """
    df_etrend = duck_read(db_path, et_sql, tuple(params))
    if not df_etrend.empty:
        mapping = {'D':'Death','IN':'Injury','M':'Malfunction'}
        df_etrend['Event'] = df_etrend['EVENT_TYPE'].map(mapping).fillna('Other')
        fig = px.line(df_etrend, x="year", y="count", color="Event", markers=True, title="Adverse Event Reports by Type Over Time", height=450)
        st.plotly_chart(fig, use_container_width=True)
        exports["event_trends"] = df_etrend
    else:
        st.info("No data available for event trends.")

# --------------------
# Demographics (filter-first)
# --------------------
with demographics_tab:
    st.subheader("Patient Demographics Analysis")
    if HAS_PATIENT:
        demographics_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY
            FROM mdr m LEFT JOIN device d ON m.MDR_REPORT_KEY=d.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        ),
        patient_data_parsed as (
            SELECT
                p.MDR_REPORT_KEY,
                p.PATIENT_SEX,
                upper(regexp_extract(p.PATIENT_AGE, '([0-9]+\\.?[0-9]*)', 1)) as age_val_str,
                upper(COALESCE(regexp_extract(p.PATIENT_AGE, '([A-Za-z]+)', 1), 'YR')) as age_unit_str
            FROM patient p JOIN base_keys b ON p.MDR_REPORT_KEY = b.MDR_REPORT_KEY
            WHERE p.PATIENT_AGE IS NOT NULL
        )
        SELECT
            PATIENT_SEX,
            CASE
                WHEN age_unit_str = 'DY' THEN try_cast(age_val_str AS REAL) / 365.25
                WHEN age_unit_str = 'MO' THEN try_cast(age_val_str AS REAL) / 12.0
                WHEN age_unit_str = 'DEC' THEN try_cast(age_val_str AS REAL) * 10.0
                ELSE try_cast(age_val_str AS REAL)
            END as age_in_years
        FROM patient_data_parsed
        WHERE age_val_str != '';
        """
        try:
            df_dem = duck_read(db_path, demographics_sql, tuple(params))
            if not df_dem.empty:
                exports["demographics"] = df_dem
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("**Patient Age Distribution (Years)**")
                    df_age = df_dem[['age_in_years']].dropna()
                    df_age = df_age[df_age['age_in_years'].between(0, 110)]
                    if not df_age.empty:
                        fig = px.histogram(df_age, x="age_in_years", nbins=20, title="Patient Age Distribution", height=450, labels={'age_in_years':'Age (Years)'})
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No valid patient age data found.")
                with c2:
                    st.markdown("**Patient Sex Distribution**")
                    df_sex = df_dem[['age_in_years']].copy()
                    df_sex['PATIENT_SEX'] = df_dem['PATIENT_SEX']
                    df_sex['Sex'] = df_sex['PATIENT_SEX'].astype(str).str.upper().map({'F':'Female','M':'Male'}).fillna('Unknown')
                    sex_counts = df_sex['Sex'].value_counts().reset_index()
                    sex_counts.columns = ['Sex', 'count']
                    if not sex_counts.empty:
                        fig = px.pie(sex_counts, names="Sex", values="count", title="Patient Sex Distribution", hole=0.3, height=450)
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No valid patient sex data found.")
            else:
                st.info("No patient demographic data found for this selection.")
        except duckdb.Error as e:
            st.error("A database error occurred while fetching demographic data.")
            st.exception(e)
    else:
        st.warning("The 'patient' table was not found in the database.")

# --------------------
# Reporter Analysis (filter-first)
# --------------------
with reporter_tab:
    st.subheader("Reporter Analysis")
    REPORTER_OCC_MAP = {
        "000":"OTHER","001":"PHYSICIAN","002":"NURSE","003":"NON-HEALTHCARE PROFESSIONAL",
        "0HP":"HEALTH PROFESSIONAL","0LP":"LAY USER/PATIENT","100":"OTHER HEALTH CARE PROFESSIONAL",
        "101":"AUDIOLOGIST","102":"DENTAL HYGIENIST","103":"DIETICIAN","104":"EMT",
        "105":"MEDICAL TECHNOLOGIST","106":"NUCLEAR MED TECH","107":"OCCUPATIONAL THERAPIST",
        "108":"PARAMEDIC","109":"PHARMACIST","110":"PHLEBOTOMIST","111":"PHYSICAL THERAPIST",
        "112":"PHYSICIAN ASSISTANT","113":"RADIOLOGIC TECHNOLOGIST","114":"RESPIRATORY THERAPIST",
        "115":"SPEECH THERAPIST","116":"DENTIST","117":"NURSE PRACTITIONER",
        "300":"OTHER CAREGIVERS","301":"(OTHER)"
    }
    c1, c2 = st.columns(2)
    with c1:
        occ_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY, COALESCE(NULLIF(TRIM(REPORTER_OCCUPATION_CODE), ''), 'UNK') AS occ
            FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY = m.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        )
        SELECT occ, COUNT(*) AS count FROM base_keys GROUP BY occ ORDER BY count DESC;
        """
        df_occ = duck_read(db_path, occ_sql, tuple(params))
        if not df_occ.empty:
            df_occ["Occupation"] = df_occ["occ"].apply(lambda c: REPORTER_OCC_MAP.get(str(c).strip().upper(), f"OTHER ({c})"))
            fig = px.pie(df_occ.head(10), names="Occupation", values="count", title="Top 10 Reporter Occupations", hole=0.3, height=450)
            st.plotly_chart(fig, use_container_width=True)
            exports["reporter_occupations"] = df_occ
        else:
            st.info("No reporter occupation data.")
    with c2:
        dow_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY, {date_pref_sql} AS dt
            FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        )
        SELECT strftime('%w', dt) as day_num, CASE strftime('%w', dt)
             WHEN '0' THEN 'Sunday' WHEN '1' THEN 'Monday' WHEN '2' THEN 'Tuesday'
             WHEN '3' THEN 'Wednesday' WHEN '4' THEN 'Thursday' WHEN '5' THEN 'Friday'
             ELSE 'Saturday' END as day,
             COUNT(DISTINCT MDR_REPORT_KEY) as count
        FROM base_keys WHERE dt IS NOT NULL GROUP BY day_num, day ORDER BY day_num;
        """
        df_dow = duck_read(db_path, dow_sql, tuple(params))
        if not df_dow.empty:
            fig = px.bar(df_dow, x="day", y="count", title="Reports by Day of Week Received", height=450)
            st.plotly_chart(fig, use_container_width=True)
            exports["reports_by_dow"] = df_dow
        else:
            st.info("No day-of-week data.")

# --------------------
# Problem Codes (Patient + Device) — binder-safe dict joins, filter-first
# --------------------
with problems_tab:
    st.subheader("Top Patient and Device Problem Terms")
    c1, c2 = st.columns(2)

    pat_dict_norm_sql = dict_cte_sql(pat_dict_mode, "patient_problem_dict") if HAS_PAT_DICT else "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"
    dev_dict_norm_sql = dict_cte_sql(dev_dict_mode, "device_problem_dict") if HAS_DEV_DICT else "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"

    with c1:
        st.markdown("**Patient Problems**")
        if HAS_PATPROB:
            pat_sql = f"""
            WITH dict_norm AS ({pat_dict_norm_sql}),
                 base_keys AS (
                    SELECT DISTINCT m.MDR_REPORT_KEY
                    FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
                    WHERE {where_sql}
                    LIMIT {PREFETCH_LIMIT}
                 ),
                 patient_norm AS (
                    SELECT p.MDR_REPORT_KEY, TRIM(code.value) AS code
                    FROM patient_problem_codes p
                    JOIN base_keys b ON b.MDR_REPORT_KEY = p.MDR_REPORT_KEY,
                         LATERAL UNNEST(str_split(COALESCE(CAST(p.PROBLEM_CODE AS VARCHAR), ''), ',')) AS code(value)
                    WHERE TRIM(code.value) <> ''
                 ),
                 joined AS (
                    SELECT COALESCE(pd.TERM, n.code) AS term
                    FROM patient_norm n
                    LEFT JOIN dict_norm pd ON pd.FDA_CODE = n.code
                 )
            SELECT term AS Term, COUNT(*) AS count
            FROM joined
            WHERE term IS NOT NULL AND term <> ''
            GROUP BY term
            ORDER BY count DESC
            LIMIT 50;
            """
            df_pat = duck_read(db_path, pat_sql, tuple(params))
            if not df_pat.empty:
                fig = px.bar(df_pat.head(20).sort_values('count'), y="Term", x="count", orientation='h', title="Top 20 Patient Problems", height=500)
                st.plotly_chart(fig, use_container_width=True)
                exports["patient_problems"] = df_pat
            else:
                st.info("No patient problem data.")
        else:
            st.info("patient_problem_codes table not present.")

    with c2:
        st.markdown("**Device Problems**")
        if HAS_FOIDEV:
            dev_sql = f"""
            WITH dict_norm AS ({dev_dict_norm_sql}),
                 base_keys AS (
                    SELECT DISTINCT m.MDR_REPORT_KEY
                    FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
                    WHERE {where_sql}
                    LIMIT {PREFETCH_LIMIT}
                 ),
                 device_norm AS (
                    SELECT d.MDR_REPORT_KEY, TRIM(code.value) AS code
                    FROM foidevproblem d
                    JOIN base_keys b ON b.MDR_REPORT_KEY = d.MDR_REPORT_KEY,
                         LATERAL UNNEST(str_split(COALESCE(CAST(d.DEVICE_PROBLEM_CODE AS VARCHAR), ''), ',')) AS code(value)
                    WHERE TRIM(code.value) <> ''
                 ),
                 joined AS (
                    SELECT COALESCE(dd.TERM, n.code) AS term
                    FROM device_norm n
                    LEFT JOIN dict_norm dd ON dd.FDA_CODE = n.code
                 )
            SELECT term AS Term, COUNT(*) AS count
            FROM joined
            WHERE term IS NOT NULL AND term <> ''
            GROUP BY term
            ORDER BY count DESC
            LIMIT 50;
            """
            df_dev = duck_read(db_path, dev_sql, tuple(params))
            if not df_dev.empty:
                fig = px.bar(df_dev.head(20).sort_values('count'), y="Term", x="count", orientation='h', title="Top 20 Device Problems", height=500)
                st.plotly_chart(fig, use_container_width=True)
                exports["device_problems"] = df_dev
            else:
                st.info("No device problem data.")
        else:
            st.info("foidevproblem table not present.")

# --------------------
# Time-to-Report (filter-first)
# --------------------
with lag_tab:
    st.subheader("Time-to-Report (Reporting Lag)")
    lag_sql = f"""
    WITH base_keys AS (
        SELECT DISTINCT m.MDR_REPORT_KEY, {event_date_sql} as evt, {date_pref_sql} as rcv
        FROM mdr m LEFT JOIN device d ON d.MDR_REPORT_KEY=m.MDR_REPORT_KEY
        WHERE {where_sql}
        LIMIT {PREFETCH_LIMIT}
    ),
    lags AS (
        SELECT date_diff('day', evt, rcv) AS lag
        FROM base_keys WHERE evt IS NOT NULL AND rcv IS NOT NULL
    )
    SELECT * FROM lags WHERE lag BETWEEN 0 AND 365;
    """
    df_lag = duck_read(db_path, lag_sql, tuple(params))
    if not df_lag.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric("Median Lag", f"{float(df_lag['lag'].median()):.1f} d")
        c2.metric("IQR", f"{float(df_lag['lag'].quantile(0.75)) - float(df_lag['lag'].quantile(0.25)):.1f} d")
        c3.metric("P90", f"{float(df_lag['lag'].quantile(0.9)):.1f} d")
        fig = px.histogram(df_lag, x="lag", nbins=50, title="Distribution of Reporting Lag (0–365 days)", height=450)
        st.plotly_chart(fig, use_container_width=True)
        exports["reporting_lag"] = df_lag
    else:
        st.info("No data for reporting lag analysis.")

# --------------------
# Narrative Analysis (filter-first)
# --------------------
with narrative_tab:
    st.subheader("Narrative Analysis (from FOI Text)")
    BASE_STOPWORDS = {"patient", "device", "report", "event", "manufacturer", "information", "unknown", "date"}

    if FOI_TEXT_COL:
        narr_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY
            FROM mdr m LEFT JOIN device d ON m.MDR_REPORT_KEY=d.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        )
        SELECT f.{FOI_TEXT_COL} AS narrative
        FROM foi f JOIN base_keys b USING (MDR_REPORT_KEY)
        WHERE f.{FOI_TEXT_COL} IS NOT NULL;
        """
        df_narr = duck_read(db_path, narr_sql, tuple(params))
        if not df_narr.empty:
            exports["raw_narrative"] = df_narr

            # Build token list
            full_text = " ".join(df_narr["narrative"].astype(str).tolist())
            stop_words = set(WordCloud().stopwords).union(BASE_STOPWORDS).union(
                {w.strip().lower() for w in extra_stop.split(",") if w.strip()}
            )
            tokens = re.findall(r"\b[a-zA-Z]{3,}\b", full_text.lower())
            words_all = [w for w in tokens if w not in stop_words]

            # Clinical-only heuristic (same source for cloud and table)
            if clinical_focus:
                clinical_suffixes = ("itis","emia","osis","algia","pathy","oma","plasia","megaly")
                words_filtered = [w for w in words_all if w.endswith(clinical_suffixes)]
            else:
                words_filtered = words_all

            c1, c2 = st.columns([1, 2])

            # ----- Table: single terms + collocations -----
            with c1:
                word_counts = Counter(words_filtered)

                bigram_counts = Counter()
                trigram_counts = Counter()
                if _NLTK_OK and len(words_filtered) >= 2:
                    try:
                        bf = BigramCollocationFinder.from_words(words_filtered)
                        tf = TrigramCollocationFinder.from_words(words_filtered)
                        bf.apply_freq_filter(2)
                        tf.apply_freq_filter(2)
                        for (a, b), cnt in bf.ngram_fd.items():
                            bigram_counts[" ".join((a, b))] = cnt
                        for (a, b, c), cnt in tf.ngram_fd.items():
                            trigram_counts[" ".join((a, b, c))] = cnt
                    except Exception:
                        bigrams = [" ".join(g) for g in zip(words_filtered, words_filtered[1:])]
                        trigrams = [" ".join(g) for g in zip(words_filtered, words_filtered[1:], words_filtered[2:])]
                        bigram_counts = Counter(bigrams)
                        trigram_counts = Counter(trigrams)
                else:
                    bigrams = [" ".join(g) for g in zip(words_filtered, words_filtered[1:])]
                    trigrams = [" ".join(g) for g in zip(words_filtered, words_filtered[1:], words_filtered[2:])]
                    bigram_counts = Counter(bigrams)
                    trigram_counts = Counter(trigrams)

                all_counts = word_counts + bigram_counts + trigram_counts
                df_phrases = (
                    pd.DataFrame(all_counts.items(), columns=["Phrase", "Count"])
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                )
                if min_ngram_count > 1:
                    df_phrases = df_phrases[df_phrases["Count"] >= min_ngram_count]

                st.markdown("**Common Phrases**")
                if not df_phrases.empty:
                    st.dataframe(df_phrases.head(50), use_container_width=True, hide_index=True, height=400)
                    exports["narrative_phrases"] = df_phrases
                else:
                    st.info("No phrase meets the threshold or clinical filter.")

            # ----- Word Cloud -----
            with c2:
                st.markdown("**Word Cloud**")
                if words_filtered:
                    wc = WordCloud(
                        width=1000, height=500, background_color="white",
                        stopwords=stop_words, collocations=False
                    ).generate(" ".join(words_filtered))
                    st.image(wc.to_array(), use_container_width=True)
                else:
                    st.info("No words left after filtering to generate a cloud.")
        else:
            st.info("No narrative text found for this selection.")
    else:
        st.warning("Narrative column not found in 'foi' table.")

# --------------------
# Raw Narratives (full FOI text table) — filter-first
# --------------------
with raw_tab:
    st.subheader("Raw Narrative Texts for Manual Review")
    if FOI_TEXT_COL:
        raw_sql = f"""
        WITH base_keys AS (
            SELECT DISTINCT m.MDR_REPORT_KEY, m.EVENT_TYPE, m.MANUFACTURER_NAME,
                   {date_pref_sql} AS DATE_PREF
            FROM mdr m
            LEFT JOIN device d ON m.MDR_REPORT_KEY = d.MDR_REPORT_KEY
            WHERE {where_sql}
            LIMIT {PREFETCH_LIMIT}
        )
        SELECT b.MDR_REPORT_KEY,
               b.DATE_PREF AS DATE_RECEIVED,
               b.EVENT_TYPE,
               b.MANUFACTURER_NAME,
               f.{FOI_TEXT_COL} AS NARRATIVE
        FROM base_keys b
        JOIN foi f ON f.MDR_REPORT_KEY = b.MDR_REPORT_KEY
        WHERE f.{FOI_TEXT_COL} IS NOT NULL;
        """
        df_raw = duck_read(db_path, raw_sql, tuple(params))
        if not df_raw.empty:
            st.dataframe(df_raw, use_container_width=True, height=520)
            exports["raw_narratives_full"] = df_raw
        else:
            st.info("No narrative data found for this selection.")
    else:
        st.warning("FOI text not available in this database.")

# --------------------
# Master Export (optimized, array_agg DISTINCT + filter-first)
# --------------------
with master_tab:
    st.subheader("Unified Master Export (MDR + Device + Demographic + Problem + Narrative)")
    st.caption("Filter-pushed-down and vectorized; output is identical in structure, much faster.")

    master_sql = f"""
    WITH base_keys AS (
        SELECT DISTINCT m.MDR_REPORT_KEY
        FROM mdr m
        LEFT JOIN device d ON m.MDR_REPORT_KEY = d.MDR_REPORT_KEY
        WHERE {where_sql}
        LIMIT {PREFETCH_LIMIT}
    ),
    base AS (
        SELECT b.MDR_REPORT_KEY, m.REPORT_NUMBER, m.EVENT_TYPE,
               {date_pref_sql} AS DATE_RECEIVED,
               COALESCE(d.MANUFACTURER_D_NAME, m.MANUFACTURER_NAME) AS MANUFACTURER,
               d.BRAND_NAME, d.GENERIC_NAME, d.MODEL_NUMBER, d.DEVICE_REPORT_PRODUCT_CODE,
               m.REPORTER_OCCUPATION_CODE
        FROM base_keys b
        JOIN mdr m USING (MDR_REPORT_KEY)
        LEFT JOIN device d USING (MDR_REPORT_KEY)
    ),
    pat AS (
        SELECT p.MDR_REPORT_KEY,
               MAX(p.PATIENT_SEX) AS PATIENT_SEX,
               MAX(p.PATIENT_AGE) AS PATIENT_AGE
        FROM patient p
        JOIN base_keys b ON p.MDR_REPORT_KEY = b.MDR_REPORT_KEY
        GROUP BY p.MDR_REPORT_KEY
    ),
    pat_prob AS (
        SELECT p.MDR_REPORT_KEY,
               array_agg(DISTINCT p.PROBLEM_CODE) AS PATIENT_PROBLEMS
        FROM patient_problem_codes p
        JOIN base_keys b ON p.MDR_REPORT_KEY = b.MDR_REPORT_KEY
        GROUP BY p.MDR_REPORT_KEY
    ),
    dev_prob AS (
        SELECT f.MDR_REPORT_KEY,
               array_agg(DISTINCT f.DEVICE_PROBLEM_CODE) AS DEVICE_PROBLEMS
        FROM foidevproblem f
        JOIN base_keys b ON f.MDR_REPORT_KEY = b.MDR_REPORT_KEY
        GROUP BY f.MDR_REPORT_KEY
    ),
    foi_text AS (
        SELECT f.MDR_REPORT_KEY,
               array_agg(DISTINCT f.{FOI_TEXT_COL}) AS FULL_NARRATIVE_LIST
        FROM foi f
        JOIN base_keys b ON f.MDR_REPORT_KEY = b.MDR_REPORT_KEY
        GROUP BY f.MDR_REPORT_KEY
    )
    SELECT b.MDR_REPORT_KEY, b.REPORT_NUMBER, b.EVENT_TYPE, b.DATE_RECEIVED,
           b.MANUFACTURER, b.BRAND_NAME, b.GENERIC_NAME, b.MODEL_NUMBER, b.DEVICE_REPORT_PRODUCT_CODE,
           b.REPORTER_OCCUPATION_CODE AS REPORTER_OCCUPATION,
           pat.PATIENT_SEX, pat.PATIENT_AGE,
           array_to_string(pat_prob.PATIENT_PROBLEMS, ', ') AS PATIENT_PROBLEMS,
           array_to_string(dev_prob.DEVICE_PROBLEMS, ', ') AS DEVICE_PROBLEMS,
           array_to_string(foi_text.FULL_NARRATIVE_LIST, ' || ') AS FULL_NARRATIVE
    FROM base b
    LEFT JOIN pat ON b.MDR_REPORT_KEY = pat.MDR_REPORT_KEY
    LEFT JOIN pat_prob ON b.MDR_REPORT_KEY = pat_prob.MDR_REPORT_KEY
    LEFT JOIN dev_prob ON b.MDR_REPORT_KEY = dev_prob.MDR_REPORT_KEY
    LEFT JOIN foi_text ON b.MDR_REPORT_KEY = foi_text.MDR_REPORT_KEY;
    """
    df_master = duck_read(db_path, master_sql, tuple(params))
    if not df_master.empty:
        st.success(f"Generated {len(df_master):,} combined records (prefetched up to {PREFETCH_LIMIT:,} MDRs).")
        st.dataframe(df_master.head(100), use_container_width=True, height=420)
        exports["master_export"] = df_master

        master_bytes = df_to_excel_bytes(df_master, sheet_name="Master_Export")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            label="⬇️ Download Master Excel Export",
            data=master_bytes,
            file_name=f"maude_master_export_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_master_{ts}"
        )
    else:
        st.info("No combined data found for this filter selection.")

# --------------------
# Per-tab Exports (sidebar)
# --------------------
ts_all = datetime.now().strftime("%Y%m%d_%H%M%S")
with export_container:
    for key, df in list(exports.items()):
        if isinstance(df, pd.DataFrame) and not df.empty:
            data = df_to_excel_bytes(df, sheet_name=key[:31])
            st.download_button(
                label=f"Download Excel ({key.replace('_',' ').title()})",
                data=data,
                file_name=f"maude_{key}_{ts_all}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_{key}_{ts_all}"
            )
