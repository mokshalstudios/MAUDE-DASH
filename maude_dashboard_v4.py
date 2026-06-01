"""
MAUDE Advanced Research Dashboard — v4
======================================

Publication-grade clinical dashboard. Adds the clinical-outcome data that v3
didn't surface and includes proper statistical tests with Wilson 95% CIs.

NEW TABS vs v3
--------------
  Clinical Outcomes       — FDA 7-harm classification (D/L/H/S/C/R/O) + Wilson CIs
  Subgroup Analysis       — forest plot of outcome rates by sex/age/year/source
  Trend Tests             — Cochran-Armitage + Mann-Kendall on yearly counts
  Device Age at Failure   — when DEVICE_AGE_TEXT available
  Geography               — by REPORTER_COUNTRY_CODE
  Sensitivity Analysis    — base case vs filtered scenarios side by side
  STROBE Report           — methods text + filter flow

Requirements
------------
  python maude_analytic_build_v2.py --db maude_final.duckdb
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from datetime import datetime
from io import BytesIO
from typing import Any, Optional

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from wordcloud import STOPWORDS, WordCloud

import maude_stats as ms

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVENT_LABELS = {"D": "Death", "IN": "Injury", "M": "Malfunction", "*": "Other"}

OUTCOME_FIELDS = [
    ("outcome_death", "Death"),
    ("outcome_life_threatening", "Life-threatening"),
    ("outcome_hospitalization", "Hospitalization"),
    ("outcome_disability", "Disability"),
    ("outcome_congenital_anomaly", "Congenital anomaly"),
    ("outcome_required_intervention", "Required intervention"),
    ("outcome_other", "Other"),
    ("any_serious_outcome", "Any serious (D/L/H/S/C/R)"),
]

REPORT_SOURCE_LABELS = {
    "M": "Manufacturer", "U": "User Facility", "D": "Distributor",
    "I": "Importer", "V": "Voluntary", "P": "Patient", "C": "Consumer",
}
REPORTER_OCC_MAP = {
    "001": "PHYSICIAN", "002": "NURSE", "003": "NON-HEALTHCARE PROFESSIONAL",
    "0HP": "HEALTH PROFESSIONAL", "0LP": "LAY USER/PATIENT",
    "100": "OTHER HEALTH CARE PROFESSIONAL", "101": "AUDIOLOGIST",
    "102": "DENTAL HYGIENIST", "103": "DIETICIAN", "104": "EMT",
    "105": "MEDICAL TECHNOLOGIST", "106": "NUCLEAR MED TECH",
    "107": "OCCUPATIONAL THERAPIST", "108": "PARAMEDIC", "109": "PHARMACIST",
    "110": "PHLEBOTOMIST", "111": "PHYSICAL THERAPIST",
    "112": "PHYSICIAN ASSISTANT", "113": "RADIOLOGIC TECHNOLOGIST",
    "114": "RESPIRATORY THERAPIST", "115": "SPEECH THERAPIST",
    "116": "DENTIST", "117": "NURSE PRACTITIONER",
}

AGE_BAND_SQL = """
CASE
  WHEN age_years_avg IS NULL THEN 'Unknown'
  WHEN age_years_avg < 1 THEN '<1 year'
  WHEN age_years_avg < 18 THEN '1-17'
  WHEN age_years_avg < 35 THEN '18-34'
  WHEN age_years_avg < 50 THEN '35-49'
  WHEN age_years_avg < 65 THEN '50-64'
  WHEN age_years_avg < 80 THEN '65-79'
  ELSE '80+'
END
"""

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(page_title="MAUDE Dashboard v4", layout="wide")
st.title("MAUDE Advanced Research Dashboard")
st.caption("v4 - publication-grade clinical analytics over the full population")


# ---------------------------------------------------------------------------
# Connection / introspection
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_con(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=True)


@st.cache_data(show_spinner=False)
def list_tables(db_path: str) -> set[str]:
    return {r[0] for r in get_con(db_path).execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()}


@st.cache_data(show_spinner=False)
def get_columns(db_path: str, table: str) -> list[str]:
    try:
        return [r[0] for r in get_con(db_path).execute(f'DESCRIBE "{table}"').fetchall()]
    except Exception:
        return []


def has_col(db_path: str, table: str, col: str) -> bool:
    return col in get_columns(db_path, table)


def query(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    return get_con(db_path).execute(sql, params).fetchdf()


def df_to_excel(df: pd.DataFrame, sheet: str = "Sheet1") -> bytes:
    LIMIT = 32_000
    df = df.copy()
    for col in df.select_dtypes(include="object").columns:
        s = df[col].astype(str)
        long = s.str.len() > LIMIT
        if long.any():
            df[col] = s.where(~long, s.str[:LIMIT] + "  ...[TRUNCATED]")
    buf = BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name=sheet[:31] or "Sheet1")
    except Exception:
        buf = BytesIO()
        buf.write(df.to_csv(index=False).encode())
    return buf.getvalue()


def _nz(v, kind=float):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return kind(0)
        return kind(v)
    except Exception:
        return kind(0)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

qp = st.query_params

st.sidebar.header("Filters")
db_path = st.sidebar.text_input("DuckDB file", value=qp.get("db", "maude_final.duckdb"))

if not os.path.exists(db_path):
    st.error(f"Database not found: {db_path}")
    st.stop()

tables = list_tables(db_path)
if "mdr_flat" not in tables:
    st.error(
        "**v4 requires the v2 analytic build.** Run:\n\n"
        "```bash\n"
        f"python maude_analytic_build_v2.py --db {db_path}\n"
        "```\n\nThen refresh."
    )
    st.stop()

clinical_ready = has_col(db_path, "mdr_flat", "any_serious_outcome")
if not clinical_ready:
    st.warning(
        "**`mdr_flat` lacks clinical-outcome fields.** Rebuild with "
        "`maude_analytic_build_v2.py` to enable Clinical Outcomes / Subgroup / "
        "Sensitivity tabs."
    )

HAS_FOI = "foi" in tables
HAS_FDP_FLAT = "flat_dev_problems" in tables
HAS_PPC_FLAT = "flat_pat_problems" in tables
HAS_DEV_DICT = "device_problem_dict" in tables
HAS_PAT_DICT = "patient_problem_dict" in tables
HAS_DEV_GLOBAL = "agg_dev_problems_global" in tables
HAS_PAT_GLOBAL = "agg_pat_problems_global" in tables

with st.sidebar.expander("Database status", expanded=False):
    info = {
        "Tables": sorted(tables),
        "Clinical outcome fields": clinical_ready,
        "Has foi (raw narratives)": HAS_FOI,
    }
    n = query(db_path, "SELECT COUNT(*) AS n FROM mdr_flat").iloc[0]["n"]
    info["mdr_flat rows"] = f"{int(n):,}"
    st.json(info)

mdr_key_input = st.sidebar.text_input("Direct MDR_REPORT_KEY lookup", value=qp.get("mdr", ""))
st.sidebar.markdown("---")

product_code_input = st.sidebar.text_input(
    "Product code (3-letter FDA code)", value=qp.get("pc", ""),
    help="Most reliable filter for a device class."
)
manufacturer_input = st.sidebar.text_input("Manufacturer contains", value=qp.get("mfg", ""))
device_terms_input = st.sidebar.text_input(
    "Device brand/generic/model contains (semicolon-separated)", value=qp.get("dev", "")
)

year_bounds = query(db_path, """
    SELECT MIN(report_year) AS lo, MAX(report_year) AS hi FROM mdr_flat
    WHERE report_year IS NOT NULL
""")
if year_bounds.empty or pd.isna(year_bounds.iloc[0]["lo"]):
    year_lo, year_hi = 2015, 2025
else:
    year_lo = int(year_bounds.iloc[0]["lo"])
    year_hi = int(year_bounds.iloc[0]["hi"])

year_range = st.sidebar.slider("Year range", min_value=year_lo, max_value=year_hi,
                                value=(year_lo, year_hi))

narrative_input = st.sidebar.text_input(
    "Narrative description contains", value=qp.get("narr", ""),
    help="Searches the first 4KB of the description text."
)

st.sidebar.markdown("---")
st.sidebar.subheader("Event types")
event_picks = []
for code, label in [("D", "Deaths"), ("IN", "Injuries"), ("M", "Malfunctions")]:
    if st.sidebar.checkbox(label, value=True, key=f"et_{code}"):
        event_picks.append(code)

st.sidebar.markdown("---")
st.sidebar.subheader("Cohort exclusions")
exclude_forwarded = st.sidebar.checkbox("Exclude 803.22(b)(2) forwarded reports", value=False)
exclude_rwd = st.sidebar.checkbox("Exclude RWD-sourced reports", value=False)
initial_only = st.sidebar.checkbox(
    "Initial reports only (exclude supplements)", value=False,
    help="Recommended for population estimates: deduplicates supplements that "
         "extend an existing report."
)

if clinical_ready:
    st.sidebar.markdown("---")
    st.sidebar.subheader("Clinical subset")
    require_implant = st.sidebar.checkbox("Implants only", value=False)
    require_serious = st.sidebar.checkbox("Serious outcomes only", value=False)
else:
    require_implant = False
    require_serious = False

st.sidebar.markdown("---")
max_preview = st.sidebar.number_input(
    "Preview / export max rows", min_value=100, max_value=1_000_000,
    value=2000, step=500,
)


# ---------------------------------------------------------------------------
# WHERE clause
# ---------------------------------------------------------------------------

def build_where(extra_exclusions: Optional[dict] = None) -> tuple[str, list[Any]]:
    ex = {
        "exclude_forwarded": exclude_forwarded,
        "exclude_rwd": exclude_rwd,
        "initial_only": initial_only,
        "require_implant": require_implant,
        "require_serious": require_serious,
        **(extra_exclusions or {}),
    }

    if mdr_key_input.strip():
        return "MDR_REPORT_KEY = ?", [mdr_key_input.strip()]

    parts: list[str] = ["report_year BETWEEN ? AND ?"]
    params: list[Any] = list(year_range)

    if product_code_input.strip():
        parts.append("product_code = ?")
        params.append(product_code_input.strip().upper())
    if manufacturer_input.strip():
        parts.append("manufacturer_l LIKE ?")
        params.append(f"%{manufacturer_input.strip().lower()}%")
    terms = [t.strip().lower() for t in device_terms_input.split(";") if t.strip()]
    if terms:
        sub: list[str] = []
        for t in terms:
            sub.append("(brand_name_l LIKE ? OR generic_name_l LIKE ? OR model_number_l LIKE ?)")
            params.extend([f"%{t}%"] * 3)
        parts.append("(" + " OR ".join(sub) + ")")
    if narrative_input.strip():
        parts.append("narrative_desc_l LIKE ?")
        params.append(f"%{narrative_input.strip().lower()}%")
    if event_picks and len(event_picks) < 3:
        placeholders = ",".join("?" for _ in event_picks)
        parts.append(f"EVENT_TYPE IN ({placeholders})")
        params.extend(event_picks)
    if ex["exclude_forwarded"]:
        parts.append("COALESCE(IS_FORWARDED_803_22_B2, FALSE) = FALSE")
    if ex["exclude_rwd"]:
        parts.append("COALESCE(IS_RWD_SOURCED, FALSE) = FALSE")
    if ex["initial_only"] and has_col(db_path, "mdr_flat", "initial_report"):
        parts.append("COALESCE(initial_report, TRUE) = TRUE")
    if ex["require_implant"] and has_col(db_path, "mdr_flat", "implant_flag"):
        parts.append("COALESCE(implant_flag, FALSE) = TRUE")
    if ex["require_serious"] and has_col(db_path, "mdr_flat", "any_serious_outcome"):
        parts.append("COALESCE(any_serious_outcome, FALSE) = TRUE")

    return " AND ".join(parts), params


where_sql, where_params = build_where()
where_params_tuple = tuple(where_params)

st.query_params.from_dict({
    "db": db_path,
    **{k: v for k, v in {
        "mdr": mdr_key_input, "pc": product_code_input,
        "mfg": manufacturer_input, "dev": device_terms_input,
        "narr": narrative_input,
    }.items() if v}
})

if not any([mdr_key_input, product_code_input, manufacturer_input,
            device_terms_input, narrative_input]):
    st.info(
        "**Enter at least one filter** in the sidebar (MDR key, product code, "
        "manufacturer, device term, or narrative search) to begin."
    )
    st.stop()


# ---------------------------------------------------------------------------
# KPI band
# ---------------------------------------------------------------------------

if clinical_ready:
    kpi_sql = f"""
        SELECT
            COUNT(*) AS n_reports,
            SUM(CASE WHEN EVENT_TYPE='D'  THEN 1 ELSE 0 END) AS n_d,
            SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS n_i,
            SUM(CASE WHEN EVENT_TYPE='M'  THEN 1 ELSE 0 END) AS n_m,
            SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS n_outcome_death,
            SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious,
            COUNT(DISTINCT REPORT_NUMBER) AS n_events
        FROM mdr_flat WHERE {where_sql};
    """
else:
    kpi_sql = f"""
        SELECT
            COUNT(*) AS n_reports,
            SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS n_d,
            SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS n_i,
            SUM(CASE WHEN EVENT_TYPE='M' THEN 1 ELSE 0 END) AS n_m,
            NULL AS n_outcome_death, NULL AS n_serious,
            COUNT(DISTINCT REPORT_NUMBER) AS n_events
        FROM mdr_flat WHERE {where_sql};
    """
kpi = query(db_path, kpi_sql, where_params_tuple)

if not kpi.empty:
    r = kpi.iloc[0]
    n_reports = _nz(r["n_reports"], int)
    n_events = _nz(r["n_events"], int)
    cols = st.columns(8)
    cols[0].metric("Reports", f"{n_reports:,}",
                   help="Total submissions (incl. supplements)")
    cols[1].metric("Unique events", f"{n_events:,}",
                   help="Distinct REPORT_NUMBER")
    cols[2].metric("Deaths (EVENT_TYPE)", f"{_nz(r['n_d'], int):,}")
    cols[3].metric("Injuries", f"{_nz(r['n_i'], int):,}")
    cols[4].metric("Malfunctions", f"{_nz(r['n_m'], int):,}")
    if clinical_ready:
        n_death = _nz(r["n_outcome_death"], int)
        n_serious = _nz(r["n_serious"], int)
        cols[5].metric("Outcome: Death", f"{n_death:,}",
                       help="From SEQUENCE_NUMBER_OUTCOME code D")
        cols[6].metric("Any serious", f"{n_serious:,}",
                       help="D/L/H/S/C/R per 21 CFR 803.3")
        if n_reports > 0:
            wci = ms.wilson_ci(n_serious, n_reports)
            cols[7].metric("% serious", wci.as_str(), help="Wilson 95% CI")

exports: dict[str, pd.DataFrame] = {}

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

TAB_LABELS = [
    "Preview", "Yearly Trends", "Event Trends",
    "Clinical Outcomes", "Demographics", "Reporter / Source", "Geography",
    "Problem Codes", "Disproportionality",
    "Subgroup Analysis", "Trend Tests", "Device Age",
    "Cohort Comparison", "Sensitivity",
    "Time-to-Report", "Narratives", "Death Deep-Dive",
    "Manufacturer Mix", "Data Quality", "Raw Narratives",
    "STROBE Report", "Master Export",
]
tabs = st.tabs(TAB_LABELS)
(t_prev, t_year, t_event, t_outcomes, t_dem, t_rep, t_geo, t_prob, t_dispro,
 t_sub, t_trend, t_age, t_cohort, t_sens, t_lag, t_narr, t_death, t_mfg,
 t_dq, t_raw, t_strobe, t_master) = tabs


def _label_events(df: pd.DataFrame, col: str = "EVENT_TYPE") -> pd.DataFrame:
    if col in df.columns:
        df = df.copy()
        df[col] = df[col].map(EVENT_LABELS).fillna(df[col])
    return df


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

with t_prev:
    st.subheader("Matching reports")
    cols_to_show = [
        "MDR_REPORT_KEY", "REPORT_NUMBER", "DATE_PREF", "EVENT_TYPE",
        "manufacturer", "BRAND_NAME", "GENERIC_NAME", "MODEL_NUMBER",
        "product_code", "device_count", "patient_count",
    ]
    if clinical_ready:
        cols_to_show += ["any_serious_outcome", "outcome_codes_raw", "implant_flag"]
    col_sql = ", ".join(cols_to_show)
    df = query(db_path, f"""
        SELECT {col_sql} FROM mdr_flat WHERE {where_sql}
        ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY
        LIMIT {int(max_preview)};
    """, where_params_tuple)
    if df.empty:
        st.info("No matching reports.")
    else:
        st.dataframe(_label_events(df), use_container_width=True, height=520)
        exports["preview"] = df

# ---------------------------------------------------------------------------
# Yearly Trends
# ---------------------------------------------------------------------------

with t_year:
    st.subheader("Yearly report volume")
    df = query(db_path, f"""
        SELECT report_year AS year, COUNT(*) AS reports
        FROM mdr_flat WHERE {where_sql} AND report_year IS NOT NULL
        GROUP BY 1 ORDER BY 1;
    """, where_params_tuple)
    if df.empty:
        st.info("No reports with a usable date.")
    else:
        fig = px.bar(df, x="year", y="reports", title="Reports per year")
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)
        exports["yearly_trends"] = df

# ---------------------------------------------------------------------------
# Event Trends
# ---------------------------------------------------------------------------

with t_event:
    st.subheader("Event types over time")
    df = query(db_path, f"""
        SELECT report_year AS year, EVENT_TYPE, COUNT(*) AS reports
        FROM mdr_flat WHERE {where_sql} AND report_year IS NOT NULL
          AND EVENT_TYPE IN ('D','IN','M')
        GROUP BY 1, 2 ORDER BY 1, 2;
    """, where_params_tuple)
    if df.empty:
        st.info("No data.")
    else:
        df["Event"] = df["EVENT_TYPE"].map(EVENT_LABELS).fillna(df["EVENT_TYPE"])
        fig = px.line(df, x="year", y="reports", color="Event", markers=True)
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)
        exports["event_trends"] = df

# ---------------------------------------------------------------------------
# Clinical Outcomes (NEW)
# ---------------------------------------------------------------------------

with t_outcomes:
    st.subheader("Clinical outcomes (per 21 CFR 803.3)")
    st.caption(
        "Patient outcome codes from `SEQUENCE_NUMBER_OUTCOME`. These are the "
        "official FDA outcome categories — more precise than EVENT_TYPE. "
        "Wilson 95% CIs shown. A report can have multiple outcomes."
    )
    if not clinical_ready:
        st.info("Re-run the v2 analytic build to enable this tab.")
    else:
        sum_terms = ", ".join(
            f"SUM(CASE WHEN {col} THEN 1 ELSE 0 END) AS {col}"
            for col, _ in OUTCOME_FIELDS
        )
        df = query(db_path, f"""
            SELECT COUNT(*) AS n_reports, {sum_terms}
            FROM mdr_flat WHERE {where_sql};
        """, where_params_tuple)
        if df.empty or df.iloc[0]["n_reports"] == 0:
            st.info("No reports in filter.")
        else:
            r = df.iloc[0]
            n_total = int(r["n_reports"])
            rows = []
            for col, label in OUTCOME_FIELDS:
                k = int(r[col]) if not pd.isna(r[col]) else 0
                ci = ms.wilson_ci(k, n_total)
                rows.append({
                    "Outcome": label, "n": k, "Total": n_total,
                    "Rate": ci.p * 100,
                    "95% CI lo": ci.lo * 100,
                    "95% CI hi": ci.hi * 100,
                    "Rate (95% CI)": ci.as_str(),
                })
            out_df = pd.DataFrame(rows)
            fig = go.Figure()
            colors = ["#d62728", "#e7298a", "#7570b3", "#1b9e77",
                     "#66a61e", "#e6ab02", "#a6761d", "#666666"]
            for i, row in out_df.iterrows():
                fig.add_trace(go.Bar(
                    y=[row["Outcome"]], x=[row["Rate"]],
                    error_x=dict(
                        type="data", symmetric=False,
                        array=[row["95% CI hi"] - row["Rate"]],
                        arrayminus=[row["Rate"] - row["95% CI lo"]],
                        color="rgba(0,0,0,0.5)", thickness=1.2,
                    ),
                    orientation="h",
                    marker_color=colors[i % len(colors)],
                    showlegend=False,
                    hovertemplate=(
                        f"{row['Outcome']}<br>n={row['n']}/{n_total}<br>"
                        f"Rate: {row['Rate (95% CI)']}<extra></extra>"
                    ),
                ))
            fig.update_layout(
                title=f"Clinical outcome rates ({n_total:,} reports) — Wilson 95% CIs",
                xaxis_title="Rate (%)",
                xaxis=dict(range=[0, max(100, float(out_df["95% CI hi"].max()) * 1.05)]),
                height=460,
                margin=dict(l=180, r=40, t=60, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(
                out_df[["Outcome", "n", "Total", "Rate (95% CI)"]],
                use_container_width=True, hide_index=True,
            )
            exports["clinical_outcomes"] = out_df

# ---------------------------------------------------------------------------
# Demographics
# ---------------------------------------------------------------------------

with t_dem:
    st.subheader("Patient demographics")
    df = query(db_path, f"""
        SELECT age_years_avg AS age, sex_list, patient_count
        FROM mdr_flat WHERE {where_sql}
          AND age_years_avg IS NOT NULL
          AND age_years_avg BETWEEN 0 AND 110;
    """, where_params_tuple)
    if df.empty:
        st.info("No demographic data.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            fig = px.histogram(df, x="age", nbins=22, title="Age (years)")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
        with c2:
            sx = df["sex_list"].fillna("").astype(str).str.upper().str[0]
            sx = sx.map({"F": "Female", "M": "Male"}).fillna("Unknown")
            counts = sx.value_counts().reset_index()
            counts.columns = ["Sex", "n"]
            fig = px.pie(counts, names="Sex", values="n", hole=0.35, title="Sex")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
        exports["demographics"] = df

# ---------------------------------------------------------------------------
# Reporter / Source
# ---------------------------------------------------------------------------

with t_rep:
    st.subheader("Reporter occupation and report source")
    c1, c2 = st.columns(2)
    with c1:
        df = query(db_path, f"""
            SELECT COALESCE(NULLIF(TRIM(REPORTER_OCCUPATION_CODE),''),'UNK') AS code,
                   COUNT(*) AS n
            FROM mdr_flat WHERE {where_sql}
            GROUP BY 1 ORDER BY n DESC;
        """, where_params_tuple)
        if not df.empty:
            df["Occupation"] = df["code"].apply(
                lambda c: REPORTER_OCC_MAP.get(str(c).upper(), f"OTHER ({c})")
            )
            top = df.head(10)
            fig = px.pie(top, names="Occupation", values="n", hole=0.35,
                        title="Top 10 reporter occupations")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
            exports["reporter_occupation"] = df
    with c2:
        df = query(db_path, f"""
            WITH base AS (
                SELECT SOURCE_TYPE FROM mdr_flat WHERE {where_sql}
                  AND SOURCE_TYPE IS NOT NULL AND TRIM(SOURCE_TYPE) <> ''
            )
            SELECT UPPER(TRIM(s.value)) AS source_code, COUNT(*) AS n
            FROM base, unnest(string_split(SOURCE_TYPE, ',')) s(value)
            WHERE TRIM(s.value) <> ''
            GROUP BY 1 ORDER BY n DESC LIMIT 15;
        """, where_params_tuple)
        if not df.empty:
            df["Source"] = df["source_code"].map(REPORT_SOURCE_LABELS).fillna(df["source_code"])
            fig = px.bar(df.sort_values("n"), y="Source", x="n", orientation="h",
                        title="Report sources")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
            exports["report_source"] = df

# ---------------------------------------------------------------------------
# Geography (NEW)
# ---------------------------------------------------------------------------

with t_geo:
    st.subheader("Reporter geography")
    if not has_col(db_path, "mdr_flat", "reporter_country_code"):
        st.info("`reporter_country_code` not populated.")
    else:
        df = query(db_path, f"""
            SELECT reporter_country_code AS country, COUNT(*) AS n
            FROM mdr_flat WHERE {where_sql}
              AND reporter_country_code IS NOT NULL
              AND reporter_country_code <> ''
            GROUP BY 1 ORDER BY n DESC;
        """, where_params_tuple)
        if df.empty:
            st.info("No country data.")
        else:
            c1, c2 = st.columns([2, 1])
            with c1:
                try:
                    fig = px.choropleth(
                        df, locations="country", color="n",
                        locationmode="ISO-3",
                        title="Reports by country", color_continuous_scale="Reds",
                    )
                    fig.update_layout(height=420, geo=dict(showframe=False))
                    st.plotly_chart(fig, use_container_width=True)
                    st.caption(
                        "Map uses ISO-3 codes; MAUDE's REPORTER_COUNTRY_CODE is "
                        "typically ISO-2, so only some entries will plot."
                    )
                except Exception:
                    pass
            with c2:
                st.dataframe(df.head(25), use_container_width=True, hide_index=True)
                exports["geography"] = df

# ---------------------------------------------------------------------------
# Problem Codes
# ---------------------------------------------------------------------------

def _dict_join_sql(dict_table: str) -> str:
    cols = get_columns(db_path, dict_table)
    if {"FDA_CODE", "TERM"}.issubset(set(cols)):
        return f'SELECT TRIM("FDA_CODE") AS FDA_CODE, TRIM("TERM") AS TERM FROM "{dict_table}"'
    merged = next((c for c in cols if "FDA_CODE" in c and "TERM" in c), None)
    if merged:
        return (
            f'SELECT TRIM(list_extract(str_split("{merged}", \',\'), 1)) AS FDA_CODE, '
            f'TRIM(list_extract(str_split("{merged}", \',\'), 2)) AS TERM FROM "{dict_table}"'
        )
    return "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"


with t_prob:
    st.subheader("Top patient + device problem terms")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Patient problems**")
        if not HAS_PPC_FLAT:
            st.info("flat_pat_problems missing.")
        else:
            dsql = _dict_join_sql("patient_problem_dict") if HAS_PAT_DICT else \
                "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"
            df = query(db_path, f"""
                WITH dict AS ({dsql}),
                     keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {where_sql})
                SELECT COALESCE(dict.TERM, fp.code) AS Term, COUNT(*) AS n
                FROM flat_pat_problems fp JOIN keys USING (MDR_REPORT_KEY)
                LEFT JOIN dict ON dict.FDA_CODE = fp.code
                GROUP BY 1 ORDER BY n DESC LIMIT 50;
            """, where_params_tuple)
            if not df.empty:
                fig = px.bar(df.head(20).sort_values("n"),
                            y="Term", x="n", orientation="h",
                            title="Top 20 patient problems")
                fig.update_layout(height=520)
                st.plotly_chart(fig, use_container_width=True)
                exports["patient_problems"] = df
    with c2:
        st.markdown("**Device problems**")
        if not HAS_FDP_FLAT:
            st.info("flat_dev_problems missing.")
        else:
            dsql = _dict_join_sql("device_problem_dict") if HAS_DEV_DICT else \
                "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"
            df = query(db_path, f"""
                WITH dict AS ({dsql}),
                     keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {where_sql})
                SELECT COALESCE(dict.TERM, fp.code) AS Term, COUNT(*) AS n
                FROM flat_dev_problems fp JOIN keys USING (MDR_REPORT_KEY)
                LEFT JOIN dict ON dict.FDA_CODE = fp.code
                GROUP BY 1 ORDER BY n DESC LIMIT 50;
            """, where_params_tuple)
            if not df.empty:
                fig = px.bar(df.head(20).sort_values("n"),
                            y="Term", x="n", orientation="h",
                            title="Top 20 device problems")
                fig.update_layout(height=520)
                st.plotly_chart(fig, use_container_width=True)
                exports["device_problems"] = df

# ---------------------------------------------------------------------------
# Disproportionality
# ---------------------------------------------------------------------------

with t_dispro:
    st.subheader("Disproportionality analysis (PRR & ROR)")
    st.caption(
        "Compares each problem code's frequency in the cohort vs the rest of "
        "the database. **Signal** = EMA-2008 rule (PRR>=2, chi2>=4, a>=3). "
        "Exploratory only — MAUDE is passive surveillance."
    )
    which = st.radio("Compute against", ["Device problems", "Patient problems"],
                    horizontal=True)
    flat_table = "flat_dev_problems" if which == "Device problems" else "flat_pat_problems"
    global_table = "agg_dev_problems_global" if which == "Device problems" else "agg_pat_problems_global"
    dict_table = "device_problem_dict" if which == "Device problems" else "patient_problem_dict"

    if flat_table not in tables or global_table not in tables:
        st.info(f"{flat_table} / {global_table} not built.")
    else:
        dsql = _dict_join_sql(dict_table) if dict_table in tables else \
            "SELECT NULL::VARCHAR AS FDA_CODE, NULL::VARCHAR AS TERM"
        df = query(db_path, f"""
            WITH dict AS ({dsql}),
                 keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {where_sql}),
                 inside AS (
                     SELECT fp.code, COUNT(DISTINCT fp.MDR_REPORT_KEY) AS n
                     FROM {flat_table} fp JOIN keys USING (MDR_REPORT_KEY)
                     GROUP BY 1 HAVING n >= 3
                 ),
                 tot AS (SELECT COUNT(*)::DOUBLE AS n FROM mdr_flat),
                 ins AS (SELECT COUNT(*)::DOUBLE AS n FROM keys),
                 joined AS (
                     SELECT inside.code, inside.n AS inside_n, g.n AS global_n
                     FROM inside JOIN {global_table} g USING (code)
                 )
            SELECT
                COALESCE(dict.TERM, joined.code) AS Term,
                joined.code AS Code,
                joined.inside_n::DOUBLE AS a,
                (ins.n - joined.inside_n)::DOUBLE AS b,
                (joined.global_n - joined.inside_n)::DOUBLE AS c,
                (tot.n - joined.global_n - ins.n + joined.inside_n)::DOUBLE AS d
            FROM joined
            LEFT JOIN dict ON dict.FDA_CODE = joined.code, tot, ins;
        """, where_params_tuple)
        if df.empty:
            st.info("Need >=3 events per code.")
        else:
            rows = []
            for _, row in df.iterrows():
                result = ms.analyze_2x2(
                    int(row["a"]), int(row["b"]), int(row["c"]), int(row["d"])
                )
                signal = ms.ema_signal(result.a, result.prr.point, result.chi2_yates)
                rows.append({
                    "Term": row["Term"], "Code": row["Code"],
                    "a (in cohort)": result.a,
                    "PRR (95% CI)": result.prr.as_str(),
                    "ROR (95% CI)": result.ror.as_str(),
                    "Chi-sq (Yates)": (round(result.chi2_yates, 2)
                                       if not math.isnan(result.chi2_yates) else None),
                    "Fisher p": (f"{result.fisher_p:.3g}"
                                 if result.fisher_p is not None else "-"),
                    "Signal": signal,
                    "_prr_sort": result.prr.point if not math.isnan(result.prr.point) else 0,
                })
            out = pd.DataFrame(rows).sort_values("_prr_sort", ascending=False).drop(columns=["_prr_sort"])
            st.dataframe(out, use_container_width=True, height=520)
            exports["disproportionality"] = out

# ---------------------------------------------------------------------------
# Subgroup Analysis (NEW) — forest plot
# ---------------------------------------------------------------------------

with t_sub:
    st.subheader("Subgroup analysis (forest plot)")
    if not clinical_ready:
        st.info("Re-run analytic build v2.")
    else:
        st.caption(
            "Outcome rate stratified by subgroup, Wilson 95% CIs. Subgroups "
            "with n<10 are flagged. Chi-square test of independence reported."
        )
        c1, c2 = st.columns(2)
        with c1:
            outcome_col = st.selectbox(
                "Outcome", [col for col, _ in OUTCOME_FIELDS],
                format_func=lambda c: dict(OUTCOME_FIELDS)[c],
            )
        with c2:
            stratify_by = st.selectbox(
                "Stratify by",
                ["Sex", "Age band", "Year", "Source type", "Country", "Reporter occupation"],
            )
        strat_sql_map = {
            "Sex": "UPPER(LEFT(COALESCE(sex_list, 'Unknown'), 1))",
            "Age band": AGE_BAND_SQL,
            "Year": "report_year::VARCHAR",
            "Source type": "UPPER(TRIM(COALESCE(SOURCE_TYPE, 'UNK')))",
            "Country": "UPPER(TRIM(COALESCE(reporter_country_code, 'UNK')))",
            "Reporter occupation": "UPPER(TRIM(COALESCE(REPORTER_OCCUPATION_CODE, 'UNK')))",
        }
        sql_expr = strat_sql_map[stratify_by]

        df = query(db_path, f"""
            SELECT {sql_expr} AS subgroup,
                   COUNT(*) AS n,
                   SUM(CASE WHEN {outcome_col} THEN 1 ELSE 0 END) AS k
            FROM mdr_flat WHERE {where_sql}
            GROUP BY 1 ORDER BY n DESC LIMIT 25;
        """, where_params_tuple)
        if df.empty:
            st.info("No data.")
        else:
            df["k"] = df["k"].astype(int)
            df["n"] = df["n"].astype(int)
            df["rate"] = 100 * df["k"] / df["n"]
            cis = [ms.wilson_ci(int(r.k), int(r.n)) for r in df.itertuples()]
            df["lo"] = [ci.lo * 100 for ci in cis]
            df["hi"] = [ci.hi * 100 for ci in cis]

            table = [[int(r.k), int(r.n) - int(r.k)] for r in df.itertuples()]
            chi2, pval, dfree = ms.chi2_independence(table)
            chi_msg = (f"Chi-sq across subgroups: chi2={chi2:.2f}, df={dfree}"
                       + (f", p={pval:.3g}" if pval is not None else ""))

            fig = go.Figure()
            for _, row in df.iterrows():
                tag = " *" if row["n"] < 10 else ""
                fig.add_trace(go.Scatter(
                    x=[row["lo"], row["hi"]],
                    y=[f"{row['subgroup']}{tag}", f"{row['subgroup']}{tag}"],
                    mode="lines",
                    line=dict(color="#1f77b4", width=2),
                    showlegend=False, hoverinfo="skip",
                ))
                fig.add_trace(go.Scatter(
                    x=[row["rate"]],
                    y=[f"{row['subgroup']}{tag}"],
                    mode="markers",
                    marker=dict(color="#1f77b4", size=10),
                    showlegend=False,
                    hovertemplate=(
                        f"{row['subgroup']}<br>"
                        f"{row['k']}/{row['n']} ({row['rate']:.1f}%)<br>"
                        f"95% CI: {row['lo']:.1f}-{row['hi']:.1f}%<extra></extra>"
                    ),
                ))
            outcome_label = dict(OUTCOME_FIELDS)[outcome_col]
            fig.update_layout(
                title=f"{outcome_label} by {stratify_by} — Wilson 95% CIs",
                xaxis_title="Rate (%)", height=480,
                margin=dict(l=180, r=40, t=60, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(chi_msg + ". * = n<10, CI unreliable.")

            out = df[["subgroup", "k", "n", "rate", "lo", "hi"]].copy()
            out.columns = ["Subgroup", "n_outcome", "Total", "Rate %",
                          "95% CI lo", "95% CI hi"]
            for c in ["Rate %", "95% CI lo", "95% CI hi"]:
                out[c] = out[c].round(2)
            st.dataframe(out, use_container_width=True, hide_index=True)
            exports["subgroup_analysis"] = out

# ---------------------------------------------------------------------------
# Trend Tests (NEW)
# ---------------------------------------------------------------------------

with t_trend:
    st.subheader("Trend tests on yearly counts")
    st.caption(
        "**Cochran-Armitage** tests for trend in a proportion across years. "
        "**Mann-Kendall** is a non-parametric monotonic-trend test on counts."
    )
    target_options = [("Total reports", None),
                      ("Deaths (EVENT_TYPE=D)", "EVENT_TYPE = 'D'")]
    if clinical_ready:
        target_options += [(f"Outcome: {label}", f"{col} = TRUE")
                          for col, label in OUTCOME_FIELDS]
    target = st.selectbox("Target", target_options, format_func=lambda t: t[0])
    target_label, target_clause = target
    counter = f"SUM(CASE WHEN {target_clause} THEN 1 ELSE 0 END)" if target_clause else "COUNT(*)"

    df = query(db_path, f"""
        SELECT report_year AS year, COUNT(*) AS n, {counter} AS k
        FROM mdr_flat WHERE {where_sql} AND report_year IS NOT NULL
        GROUP BY 1 ORDER BY 1;
    """, where_params_tuple)
    if df.empty or len(df) < 2:
        st.info("Need >= 2 years.")
    else:
        df["k"] = df["k"].astype(int)
        df["n"] = df["n"].astype(int)
        ca = ms.cochran_armitage_trend(df["k"].tolist(), df["n"].tolist(),
                                      df["year"].tolist())
        mk = ms.mann_kendall(df["k"].tolist())
        c1, c2 = st.columns(2)
        with c1:
            st.metric("Cochran-Armitage", f"z = {ca.statistic:.2f}",
                     help=f"Direction: {ca.direction}." +
                          (f" p = {ca.p_value:.4g}" if ca.p_value is not None else ""))
            st.caption(ca.note)
        with c2:
            st.metric("Mann-Kendall", f"S = {mk.statistic}",
                     help=f"Direction: {mk.direction}." +
                          (f" p = {mk.p_value:.4g}" if mk.p_value is not None else ""))
            st.caption(mk.note)

        df["rate"] = 100 * df["k"] / df["n"].replace(0, 1)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["year"], y=df["k"], name="Count",
                            marker_color="#1f77b4"))
        fig.add_trace(go.Scatter(x=df["year"], y=df["rate"], name="Rate (%)",
                                yaxis="y2", mode="lines+markers",
                                line=dict(color="#d62728", width=2)))
        fig.update_layout(
            title=f"{target_label} per year",
            yaxis=dict(title="Count"),
            yaxis2=dict(title="Rate (%)", overlaying="y", side="right"),
            height=440, legend=dict(x=0.01, y=0.99),
        )
        st.plotly_chart(fig, use_container_width=True)
        exports["trend_tests"] = df

# ---------------------------------------------------------------------------
# Device Age at Failure (NEW)
# ---------------------------------------------------------------------------

with t_age:
    st.subheader("Device age at failure")
    if not has_col(db_path, "mdr_flat", "device_age_days"):
        st.info("`device_age_days` not present.")
    else:
        df = query(db_path, f"""
            SELECT device_age_days, device_age_text_raw, EVENT_TYPE,
                   any_serious_outcome
            FROM mdr_flat
            WHERE {where_sql} AND device_age_days IS NOT NULL
              AND device_age_days BETWEEN 0 AND 36525;
        """, where_params_tuple)
        if df.empty:
            st.info(
                "No device-age data in this filter. MAUDE's DEVICE_AGE_TEXT is "
                "frequently unpopulated."
            )
        else:
            df["age_years"] = df["device_age_days"] / 365.25
            c1, c2 = st.columns(2)
            with c1:
                fig = px.histogram(
                    df, x="age_years", nbins=40,
                    title=f"Device age at failure ({len(df):,} with data)",
                    labels={"age_years": "Device age (years)"},
                )
                fig.update_layout(height=420)
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                sorted_ages = df["age_years"].sort_values().values
                cum = pd.DataFrame({
                    "age_years": sorted_ages,
                    "cumulative": (1 + pd.Series(range(len(sorted_ages)))) / len(sorted_ages) * 100,
                })
                fig = px.line(cum, x="age_years", y="cumulative",
                             title="Cumulative distribution",
                             labels={"age_years": "Device age (years)",
                                     "cumulative": "Cumulative %"})
                fig.update_layout(height=420)
                st.plotly_chart(fig, use_container_width=True)
            stats = df["age_years"].describe()
            sumrow = pd.DataFrame([{
                "n with data": len(df),
                "Median (y)": round(stats["50%"], 2),
                "IQR (y)": f"{stats['25%']:.2f}-{stats['75%']:.2f}",
                "Mean (y)": round(stats["mean"], 2),
                "P90 (y)": round(df["age_years"].quantile(0.9), 2),
            }])
            st.dataframe(sumrow, use_container_width=True, hide_index=True)
            exports["device_age"] = df

# ---------------------------------------------------------------------------
# Cohort Comparison
# ---------------------------------------------------------------------------

with t_cohort:
    st.subheader("Cohort comparison")
    st.caption("Chi-sq test of independence reported.")
    split_by = st.radio("Split by", ["Year range", "Manufacturer substring", "Event type"],
                       horizontal=True)
    cohort_a_clause = cohort_b_clause = None
    cohort_a_params: list = []
    cohort_b_params: list = []
    label_a = label_b = None
    if split_by == "Year range":
        c1, c2 = st.columns(2)
        mid = (year_lo + year_hi) // 2
        with c1:
            ay = st.slider("Cohort A", year_lo, year_hi, (year_lo, mid))
        with c2:
            by = st.slider("Cohort B", year_lo, year_hi, (mid + 1, year_hi))
        cohort_a_clause = "report_year BETWEEN ? AND ?"
        cohort_a_params = list(ay)
        cohort_b_clause = "report_year BETWEEN ? AND ?"
        cohort_b_params = list(by)
        label_a, label_b = f"{ay[0]}-{ay[1]}", f"{by[0]}-{by[1]}"
    elif split_by == "Manufacturer substring":
        c1, c2 = st.columns(2)
        with c1:
            label_a = st.text_input("Cohort A manufacturer contains", "")
        with c2:
            label_b = st.text_input("Cohort B manufacturer contains", "")
        if label_a and label_b:
            cohort_a_clause = "manufacturer_l LIKE ?"
            cohort_a_params = [f"%{label_a.lower()}%"]
            cohort_b_clause = "manufacturer_l LIKE ?"
            cohort_b_params = [f"%{label_b.lower()}%"]
    else:
        c1, c2 = st.columns(2)
        with c1:
            label_a = st.selectbox("Cohort A event type", ["D", "IN", "M"], index=0)
        with c2:
            label_b = st.selectbox("Cohort B event type", ["D", "IN", "M"], index=1)
        cohort_a_clause = "EVENT_TYPE = ?"
        cohort_a_params = [label_a]
        cohort_b_clause = "EVENT_TYPE = ?"
        cohort_b_params = [label_b]

    if cohort_a_clause and cohort_b_clause and label_a and label_b:
        rows_a = query(db_path,
            f"SELECT EVENT_TYPE, COUNT(*) AS n FROM mdr_flat "
            f"WHERE {where_sql} AND {cohort_a_clause} GROUP BY EVENT_TYPE",
            tuple(list(where_params) + list(cohort_a_params)))
        rows_b = query(db_path,
            f"SELECT EVENT_TYPE, COUNT(*) AS n FROM mdr_flat "
            f"WHERE {where_sql} AND {cohort_b_clause} GROUP BY EVENT_TYPE",
            tuple(list(where_params) + list(cohort_b_params)))
        if rows_a.empty and rows_b.empty:
            st.info("No data in either cohort.")
        else:
            rows_a["cohort"] = str(label_a)
            rows_b["cohort"] = str(label_b)
            df = pd.concat([rows_a, rows_b], ignore_index=True)
            df["Event"] = df["EVENT_TYPE"].map(EVENT_LABELS).fillna(df["EVENT_TYPE"])
            fig = px.bar(df, x="Event", y="n", color="cohort", barmode="group",
                        title=f"{label_a} vs {label_b}")
            fig.update_layout(height=420)
            st.plotly_chart(fig, use_container_width=True)
            piv = df.pivot_table(index="Event", columns="cohort",
                                values="n", fill_value=0)
            table = piv.values.tolist()
            chi2, pval, dfree = ms.chi2_independence(table)
            for c in list(piv.columns):
                tot = piv[c].sum()
                piv[f"{c} %"] = (piv[c] / tot * 100).round(1) if tot else 0.0
            st.dataframe(piv, use_container_width=True)
            st.caption(
                f"Chi-sq: chi2={chi2:.2f}, df={dfree}"
                + (f", **p={pval:.4g}**" if pval is not None else "")
            )
            exports["cohort_comparison"] = piv.reset_index()

# ---------------------------------------------------------------------------
# Sensitivity Analysis (NEW)
# ---------------------------------------------------------------------------

with t_sens:
    st.subheader("Sensitivity analysis")
    st.caption(
        "Re-run the same calculation under different exclusion criteria. "
        "Tests robustness of headline numbers to standard MAUDE caveats."
    )
    scenarios = [
        ("Base case (current sidebar)", {}),
        ("Exclude forwarded", {"exclude_forwarded": True}),
        ("Exclude RWD", {"exclude_rwd": True}),
        ("Initial reports only", {"initial_only": True}),
        ("Conservative: forwarded + RWD + initial-only",
         {"exclude_forwarded": True, "exclude_rwd": True, "initial_only": True}),
    ]
    sens_rows = []
    for label, extras in scenarios:
        w, p = build_where(extras)
        kp = query(db_path, f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS deaths
                   {", SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS serious" if clinical_ready else ""}
            FROM mdr_flat WHERE {w};
        """, tuple(p))
        if not kp.empty:
            r = kp.iloc[0]
            row = {
                "Scenario": label,
                "Reports (n)": _nz(r["n"], int),
                "Event-type Deaths": _nz(r["deaths"], int),
            }
            if clinical_ready and "serious" in r.index:
                row["Serious (outcome)"] = _nz(r["serious"], int)
                if row["Reports (n)"] > 0:
                    wci = ms.wilson_ci(row["Serious (outcome)"], row["Reports (n)"])
                    row["% Serious (95% CI)"] = wci.as_str()
            sens_rows.append(row)
    if sens_rows:
        sdf = pd.DataFrame(sens_rows)
        st.dataframe(sdf, use_container_width=True, hide_index=True)
        exports["sensitivity"] = sdf

# ---------------------------------------------------------------------------
# Time-to-Report
# ---------------------------------------------------------------------------

with t_lag:
    st.subheader("Reporting lag (event -> received)")
    df = query(db_path, f"""
        SELECT lag_days AS lag FROM mdr_flat
        WHERE {where_sql} AND lag_days IS NOT NULL;
    """, where_params_tuple)
    if df.empty:
        st.info("No lag data.")
    else:
        med = float(df["lag"].median())
        iqr = float(df["lag"].quantile(0.75) - df["lag"].quantile(0.25))
        p90 = float(df["lag"].quantile(0.9))
        c1, c2, c3 = st.columns(3)
        c1.metric("Median", f"{med:.0f} d")
        c2.metric("IQR", f"{iqr:.0f} d")
        c3.metric("P90", f"{p90:.0f} d")
        fig = px.histogram(df, x="lag", nbins=60,
                          title="Lag distribution (0-365 days)")
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)
        exports["reporting_lag"] = df

# ---------------------------------------------------------------------------
# Narratives
# ---------------------------------------------------------------------------

with t_narr:
    st.subheader("Narrative analysis")
    extra_stop = st.text_input("Extra stopwords (comma-separated)", value="")
    min_count = st.slider("Min phrase count", 1, 100, 10)
    df = query(db_path, f"""
        SELECT narrative_desc FROM mdr_flat
        WHERE {where_sql} AND narrative_desc IS NOT NULL;
    """, where_params_tuple)
    if df.empty:
        st.info("No descriptions in scope.")
    else:
        stop = set(STOPWORDS) | {
            "patient", "device", "report", "event", "manufacturer",
            "information", "unknown", "date", "received", "stated",
            "indicated", "reportedly",
        } | {w.strip().lower() for w in extra_stop.split(",") if w.strip()}
        tok_re = re.compile(r"\b[a-zA-Z]{3,}\b")
        unigrams: Counter = Counter()
        bigrams: Counter = Counter()
        trigrams: Counter = Counter()
        for txt in df["narrative_desc"].astype(str):
            toks = [w.lower() for w in tok_re.findall(txt)]
            toks = [w for w in toks if w not in stop]
            unigrams.update(toks)
            bigrams.update(" ".join(g) for g in zip(toks, toks[1:]))
            trigrams.update(" ".join(g) for g in zip(toks, toks[1:], toks[2:]))
        combined = unigrams + bigrams + trigrams
        phrases = (
            pd.DataFrame(combined.items(), columns=["Phrase", "Count"])
            .sort_values("Count", ascending=False).reset_index(drop=True)
        )
        if min_count > 1:
            phrases = phrases[phrases["Count"] >= min_count]
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown("**Top phrases**")
            if phrases.empty:
                st.info("Nothing above threshold.")
            else:
                st.dataframe(phrases.head(60), hide_index=True,
                            use_container_width=True, height=460)
                exports["narrative_phrases"] = phrases
        with c2:
            st.markdown("**Word cloud**")
            if unigrams:
                wc = WordCloud(width=1000, height=500, background_color="white",
                              stopwords=stop, collocations=False).generate_from_frequencies(
                    dict(unigrams.most_common(300)))
                st.image(wc.to_array(), use_container_width=True)

# ---------------------------------------------------------------------------
# Death Deep-Dive
# ---------------------------------------------------------------------------

with t_death:
    st.subheader("Death event deep-dive")
    death_clause = (
        "(EVENT_TYPE = 'D' OR COALESCE(outcome_death, FALSE) = TRUE)"
        if clinical_ready else "EVENT_TYPE = 'D'"
    )
    df = query(db_path, f"""
        SELECT MDR_REPORT_KEY, DATE_PREF, manufacturer,
               BRAND_NAME, GENERIC_NAME, product_code, narrative_desc
        FROM mdr_flat WHERE {where_sql} AND {death_clause}
        ORDER BY DATE_PREF DESC NULLS LAST LIMIT 5000;
    """, where_params_tuple)
    if df.empty:
        st.info("No death reports.")
    else:
        st.metric("Death reports", f"{len(df):,}")
        st.dataframe(df, use_container_width=True, height=520)
        exports["deaths"] = df

# ---------------------------------------------------------------------------
# Manufacturer Mix
# ---------------------------------------------------------------------------

with t_mfg:
    st.subheader("Manufacturer concentration")
    df = query(db_path, f"""
        SELECT manufacturer, COUNT(*) AS n
        FROM mdr_flat WHERE {where_sql} AND manufacturer IS NOT NULL
        GROUP BY 1 ORDER BY n DESC;
    """, where_params_tuple)
    if df.empty:
        st.info("No manufacturer data.")
    else:
        tot = float(df["n"].sum())
        df["share_pct"] = (df["n"] / tot * 100).round(2)
        hhi = float(((df["n"] / tot * 100) ** 2).sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Manufacturers", f"{len(df):,}")
        c2.metric("Top-1 share", f"{df['share_pct'].iloc[0]:.1f}%")
        c3.metric("HHI", f"{hhi:,.0f}")
        st.caption("HHI > 2,500 = highly concentrated.")
        fig = px.bar(df.head(20).sort_values("n"),
                    y="manufacturer", x="n", orientation="h",
                    title="Top 20 manufacturers")
        fig.update_layout(height=560)
        st.plotly_chart(fig, use_container_width=True)
        exports["manufacturer_mix"] = df

# ---------------------------------------------------------------------------
# Data Quality
# ---------------------------------------------------------------------------

with t_dq:
    st.subheader("Data quality")
    df = query(db_path, f"""
        SELECT
            AVG(CASE WHEN REPORT_NUMBER IS NULL OR TRIM(REPORT_NUMBER)='' THEN 0 ELSE 1 END)::DOUBLE AS report_number,
            AVG(CASE WHEN EVENT_TYPE IS NULL OR TRIM(EVENT_TYPE)='' THEN 0 ELSE 1 END)::DOUBLE AS event_type,
            AVG(CASE WHEN DATE_RECEIVED_D IS NULL THEN 0 ELSE 1 END)::DOUBLE AS date_received,
            AVG(CASE WHEN DATE_OF_EVENT_D IS NULL THEN 0 ELSE 1 END)::DOUBLE AS date_of_event,
            AVG(CASE WHEN manufacturer IS NULL OR TRIM(manufacturer)='' THEN 0 ELSE 1 END)::DOUBLE AS manufacturer,
            AVG(CASE WHEN product_code IS NULL OR TRIM(product_code)='' THEN 0 ELSE 1 END)::DOUBLE AS product_code,
            AVG(CASE WHEN REPORTER_OCCUPATION_CODE IS NULL OR TRIM(REPORTER_OCCUPATION_CODE)='' THEN 0 ELSE 1 END)::DOUBLE AS reporter_occupation,
            AVG(CASE WHEN SOURCE_TYPE IS NULL OR TRIM(SOURCE_TYPE)='' THEN 0 ELSE 1 END)::DOUBLE AS source_type,
            AVG(CASE WHEN has_narrative THEN 1.0 ELSE 0.0 END) AS has_narrative,
            AVG(CASE WHEN is_supplement THEN 1.0 ELSE 0.0 END) AS supplements,
            AVG(CASE WHEN IS_RWD_SOURCED THEN 1.0 ELSE 0.0 END) AS rwd_sourced,
            AVG(CASE WHEN IS_FORWARDED_803_22_B2 THEN 1.0 ELSE 0.0 END) AS forwarded,
            AVG(CASE WHEN HAS_REDACTION_B4 THEN 1.0 ELSE 0.0 END) AS redaction_b4,
            AVG(CASE WHEN HAS_REDACTION_B6 THEN 1.0 ELSE 0.0 END) AS redaction_b6
        FROM mdr_flat WHERE {where_sql};
    """, where_params_tuple)
    if not df.empty:
        s = df.iloc[0]
        comp = pd.DataFrame({
            "Field": list(s.index),
            "Rate (%)": (s.values.astype(float) * 100).round(1),
        })
        fig = px.bar(comp.sort_values("Rate (%)"),
                    x="Rate (%)", y="Field", orientation="h",
                    range_x=[0, 100],
                    title="Field completeness / flag rates (%)")
        fig.update_layout(height=520)
        st.plotly_chart(fig, use_container_width=True)
        exports["data_quality"] = comp

# ---------------------------------------------------------------------------
# Raw Narratives
# ---------------------------------------------------------------------------

with t_raw:
    st.subheader("Raw narratives (full text via foi)")
    if not HAS_FOI:
        st.info("foi table missing.")
    else:
        type_filter = ""
        if has_col(db_path, "foi", "TEXT_TYPE_CODE"):
            wanted = st.multiselect(
                "Text types", ["D", "N", "M"], default=["D", "N", "M"],
                format_func=lambda c: {
                    "D": "D - description",
                    "N": "N - manufacturer narrative",
                    "M": "M - additional mfg narrative",
                }[c],
            )
            if wanted:
                type_filter = "AND f.TEXT_TYPE_CODE IN (" + ",".join(f"'{w}'" for w in wanted) + ")"
        df = query(db_path, f"""
            WITH keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {where_sql})
            SELECT f.MDR_REPORT_KEY, f.TEXT_TYPE_CODE, f.FOI_TEXT
            FROM foi f JOIN keys USING (MDR_REPORT_KEY)
            WHERE f.FOI_TEXT IS NOT NULL {type_filter}
            LIMIT {int(max_preview)};
        """, where_params_tuple)
        if df.empty:
            st.info("No narratives.")
        else:
            st.dataframe(df, use_container_width=True, height=520)
            exports["raw_narratives"] = df

# ---------------------------------------------------------------------------
# STROBE Report (NEW) — auto-generate methods text
# ---------------------------------------------------------------------------

with t_strobe:
    st.subheader("STROBE-style reproducibility report")
    st.caption(
        "Auto-generated methods paragraph and filter description matching "
        "exactly what's being analysed. Copy-paste into a manuscript."
    )

    # Total filtered count
    n_total = int(query(db_path, f"SELECT COUNT(*) AS n FROM mdr_flat WHERE {where_sql}",
                       where_params_tuple).iloc[0]["n"])
    n_unique = int(query(db_path,
        f"SELECT COUNT(DISTINCT REPORT_NUMBER) AS n FROM mdr_flat WHERE {where_sql}",
        where_params_tuple).iloc[0]["n"])

    # Build filter description
    filter_parts = [
        f"Date range: {year_range[0]}-{year_range[1]} (by preferred report date, "
        "favouring DATE_RECEIVED with fall-back to REPORT_DATE)."
    ]
    if product_code_input.strip():
        filter_parts.append(f"FDA product classification code: '{product_code_input.strip().upper()}'.")
    if manufacturer_input.strip():
        filter_parts.append(f"Manufacturer name contains: '{manufacturer_input.strip()}' (case-insensitive).")
    if device_terms_input.strip():
        terms = [t.strip() for t in device_terms_input.split(";") if t.strip()]
        filter_parts.append(
            f"Brand/generic/model name contains any of: " +
            ", ".join(f"'{t}'" for t in terms) + " (case-insensitive)."
        )
    if narrative_input.strip():
        filter_parts.append(f"Event description contains: '{narrative_input.strip()}'.")
    if event_picks and len(event_picks) < 3:
        event_names = [EVENT_LABELS[c] for c in event_picks]
        filter_parts.append(f"Event types included: {', '.join(event_names)}.")
    if exclude_forwarded:
        filter_parts.append(
            "Excluded reports forwarded under 21 CFR 803.22(b)(2) "
            "(manufacturer/importer forwarded reports about devices not "
            "of their own manufacture)."
        )
    if exclude_rwd:
        filter_parts.append("Excluded RWD-sourced reports (21 CFR 803.19 exemption).")
    if initial_only:
        filter_parts.append(
            "Initial reports only — supplements (where SUPPLEMENT_NUMBER is "
            "populated) were excluded to avoid double-counting events that "
            "received follow-up submissions."
        )
    if require_implant:
        filter_parts.append("Restricted to reports with IMPLANT_FLAG='Y'.")
    if require_serious:
        filter_parts.append(
            "Restricted to reports with at least one serious patient outcome "
            "(D, L, H, S, C, or R per 21 CFR 803.3)."
        )

    methods = f"""## Methods

We performed a retrospective analysis of the U.S. FDA Manufacturer and User
Facility Device Experience (MAUDE) database, using the publicly distributed
quarterly downloadable files. After downloading the MDR, device, patient,
foitext, foidevproblem, and patientproblemcode files, raw text data were
normalised, multi-line foitext records were reassembled by a state-machine
parser, and the data were loaded into a DuckDB analytic database.

A denormalised analytic table (`mdr_flat`) was created with one row per
medical device report (MDR_REPORT_KEY). Patient-level fields, including
SEQUENCE_NUMBER_OUTCOME (the FDA-defined patient outcome code per 21 CFR
803.3), were aggregated to the report level. Device fields were attached
from the first device record per report (ordered by DEVICE_EVENT_KEY).

**Inclusion criteria.** {' '.join(filter_parts)}

**Outcomes.** Patient-level outcomes were extracted from SEQUENCE_NUMBER_OUTCOME
and dichotomised into the seven FDA categories: death (D), life-threatening
(L), hospitalization (H), disability (S), congenital anomaly (C), required
intervention (R), and other (O). A composite "any serious outcome" was
defined as any of D, L, H, S, C, or R.

**Statistical analysis.** Proportions are presented with Wilson 95%
confidence intervals. Comparisons between independent groups used the
chi-square test of independence (Fisher's exact test for tables with any
expected cell count < 10). For disproportionality analyses, the
proportional reporting ratio (PRR) and reporting odds ratio (ROR) were
calculated with 95% confidence intervals on the log scale, using a 0.5
continuity correction for zero cells. The Yates-corrected chi-square
statistic was reported alongside; an exploratory signal was defined per
EMA 2008 criteria (PRR ≥ 2, χ² ≥ 4, with at least 3 events in the cohort).
Trends over time were assessed using the Cochran-Armitage test for trend
in proportions and the Mann-Kendall non-parametric trend test on yearly
counts. All analyses are exploratory; MAUDE is a passive surveillance
system without a defined denominator of exposed devices, and reporting is
subject to under-reporting, stimulated reporting, and selection effects.

**Cohort size.** {n_total:,} reports met the inclusion criteria
({n_unique:,} unique events by REPORT_NUMBER).

**Software.** Analyses were performed using Python (DuckDB for storage and
query, pandas for tabular work, Plotly for figures) using the open-source
MAUDE-Dash v4 dashboard.
"""

    st.markdown(methods)
    st.download_button(
        "Download methods (.md)",
        data=methods.encode(),
        file_name=f"maude_methods_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
        mime="text/markdown",
    )

    # SQL filter dump for reproducibility
    st.markdown("### Exact filter (for reproducibility)")
    st.code(
        f"-- Applied to mdr_flat\nSELECT * FROM mdr_flat WHERE {where_sql};\n"
        f"-- Bound params (in order):\n-- {where_params}",
        language="sql",
    )

# ---------------------------------------------------------------------------
# Master Export
# ---------------------------------------------------------------------------

with t_master:
    st.subheader("Master export")
    st.caption("Direct dump of `mdr_flat` filtered to your selection.")
    df = query(db_path, f"""
        SELECT * FROM mdr_flat WHERE {where_sql}
        ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY;
    """, where_params_tuple)
    if df.empty:
        st.info("No data.")
    else:
        st.success(f"{len(df):,} rows — full filtered population.")
        st.dataframe(df.head(500), use_container_width=True, height=420)
        exports["master_export"] = df
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "Download master (.xlsx)",
            data=df_to_excel(df, "Master"),
            file_name=f"maude_master_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_master_{ts}",
        )

# ---------------------------------------------------------------------------
# Sidebar exports
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("---")
    st.subheader("Downloads")
    st.caption("Visit a tab to populate its export.")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for key, df in exports.items():
        if isinstance(df, pd.DataFrame) and not df.empty:
            st.download_button(
                label=f"Download {key.replace('_', ' ').title()} (.xlsx)",
                data=df_to_excel(df, sheet=key),
                file_name=f"maude_{key}_{ts}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_{key}_{ts}",
            )
