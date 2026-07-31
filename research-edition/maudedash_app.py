"""
MaudeDash — Research Edition
============================

Streamlit dashboard over the full FDA MAUDE corpus, for researchers running
against a locally built DuckDB (the complete ~73 GB database, including
untruncated narratives and every raw staging table).

For the public, zero-install tool see the web tier at https://maudedash.com,
which runs the same analyses in the browser over a 1.4 GB Parquet extract.

This supersedes maude_dashboard_v4.py. The v4 file is retained unmodified for
provenance, because it produced the numbers in the associated publication.
Behavioural differences that can change results are listed under
"Corrections" below and are surfaced in the app's Methods panel.

Corrections vs v4
-----------------
1. Disproportionality comparator. v4 divided by every row in mdr_flat. The FDA
   problem-code files begin in 2015, so ~20% of the corpus cannot carry a code;
   including those rows inflated every PRR by ~25% and manufactured EMA signals
   at the PRR >= 2 threshold. The comparator is now restricted to code-eligible
   reports, and the figure used is stated on screen.
2. Yates chi-square is computed on observed counts with the correction clamped
   at zero (see maude_stats.py).
3. Mann-Kendall applies the standard tie correction.
4. PRR/ROR intervals use the exact 95% z rather than 1.96.
5. The "Death reports" figure is a true COUNT; v4 displayed len(df) after a
   LIMIT 5000, so any cohort with more than 5,000 deaths silently read "5,000".

Engineering differences
-----------------------
* Only the selected analysis runs. Streamlit executes every st.tabs body on
  every interaction, so v4 fired ~30 queries against a 20.7M-row table per
  click and then serialised every result to XLSX in the sidebar. This edition
  renders one analysis at a time and builds an export only when asked.
* Every query is cached on (database, SQL, parameters).
* No unbounded SELECT *.

Usage
-----
    pip install -r requirements.txt
    streamlit run maudedash_app.py -- --db /path/to/maude_final.duckdb
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from io import BytesIO
from typing import Any, Optional

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import maude_stats as ms

APP_NAME = "MaudeDash"
APP_TAGLINE = "FDA MAUDE adverse event analytics"
VERSION = "5.0"
PAPER_URL = "https://pubmed.ncbi.nlm.nih.gov/42232423/"
REPO_URL = "https://github.com/mokshalstudios/MAUDE-DASH"
SITE_URL = "https://maudedash.com"

# The FDA device / narrative / problem-code files begin in this year. Reports
# before it exist in the MDR master but carry no device attributes at all.
CODE_ELIGIBLE_FROM_YEAR = 2015

PASSIVE_CAVEAT = (
    "MAUDE is passive surveillance with no denominator of exposed devices. "
    "Figures here are proportions of **reports**, not population incidence rates."
)

EVENT_LABELS = {"D": "Death", "IN": "Injury", "M": "Malfunction", "*": "Other"}

OUTCOMES = [
    ("outcome_death", "Death", "#8B1A1A"),
    ("outcome_life_threatening", "Life-threatening", "#C0392B"),
    ("outcome_hospitalization", "Hospitalization", "#D9740B"),
    ("outcome_disability", "Disability", "#7C3AED"),
    ("outcome_congenital_anomaly", "Congenital anomaly", "#0E7490"),
    ("outcome_required_intervention", "Required intervention", "#2563EB"),
    ("outcome_other", "Other", "#64748B"),
    ("any_serious_outcome", "Any serious (D/L/H/S/C/R)", "#0B3C5D"),
]

SOURCE_LABELS = {
    "M": "Manufacturer", "U": "User facility", "D": "Distributor",
    "I": "Importer", "V": "Voluntary", "P": "Patient", "C": "Consumer",
}

OCCUPATION = {
    "001": "Physician", "002": "Nurse", "003": "Non-healthcare professional",
    "0HP": "Health professional", "0LP": "Lay user / patient",
    "100": "Other healthcare professional", "101": "Audiologist",
    "102": "Dental hygienist", "103": "Dietician", "104": "EMT",
    "105": "Medical technologist", "106": "Nuclear medicine technologist",
    "107": "Occupational therapist", "108": "Paramedic", "109": "Pharmacist",
    "110": "Phlebotomist", "111": "Physical therapist",
    "112": "Physician assistant", "113": "Radiologic technologist",
    "114": "Respiratory therapist", "115": "Speech therapist",
    "116": "Dentist", "117": "Nurse practitioner",
}

COLUMN_LABELS = {
    "MDR_REPORT_KEY": "MDR key", "REPORT_NUMBER": "Report no.",
    "EVENT_TYPE": "Event type", "DATE_PREF": "Date",
    "manufacturer": "Manufacturer", "BRAND_NAME": "Brand",
    "GENERIC_NAME": "Generic name", "MODEL_NUMBER": "Model",
    "product_code": "Product code", "device_count": "Devices",
    "patient_count": "Patients", "any_serious_outcome": "Serious outcome",
    "outcome_codes_raw": "Outcome codes", "implant_flag": "Implant",
    "narrative_desc": "Event description", "narrative_mfg": "Mfr narrative",
    "age_years_avg": "Mean age (y)", "sex_list": "Sex",
    "reporter_country_code": "Country", "lag_days": "Lag (days)",
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

PLOT_TEMPLATE = "plotly_white"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def resolve_db_path() -> str:
    """Database location, from --db, MAUDEDASH_DB, or the working directory.

    v4 exposed this as a sidebar text input and wrote it into the URL, which
    leaked the server's filesystem layout to anyone using a shared instance.
    It is now configuration, not user input.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db", default=None)
    known, _ = parser.parse_known_args(sys.argv[1:])
    if known.db:
        return known.db
    return os.environ.get("MAUDEDASH_DB", "maude_final.duckdb")


DB_PATH = resolve_db_path()

st.set_page_config(
    page_title=f"{APP_NAME} — {APP_TAGLINE}",
    page_icon=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "favicon.png")
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "assets", "favicon.png")) else "🩺",
    layout="wide",
    menu_items={
        "About": (
            f"**{APP_NAME} {VERSION}** — {APP_TAGLINE}\n\n"
            f"Publication-grade analytics over the U.S. FDA Manufacturer and User "
            f"Facility Device Experience database.\n\n"
            f"Paper: {PAPER_URL}\n\nSource: {REPO_URL}\n\nWeb tool: {SITE_URL}\n\n"
            f"{PASSIVE_CAVEAT}"
        ),
        "Get help": REPO_URL,
        "Report a Bug": f"{REPO_URL}/issues",
    },
)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_con(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=True)


@st.cache_data(show_spinner=False, ttl=3600)
def q(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    """Cached query, executed on a per-call cursor.

    Two separate problems are being avoided here.

    Caching: v4 re-ran every query on every widget interaction.

    Concurrency: the connection is @st.cache_resource, so it is shared by every
    browser session, and Streamlit runs each session in its own thread. DuckDB
    keeps the pending result on the connection, so a second thread's execute()
    destroys the first thread's result before it is fetched. Measured on duckdb
    1.1.3, four threads issuing the same query on one shared connection got 776
    of 800 results back EMPTY; routing each call through con.cursor() returned
    800 of 800 correctly. Cursors are cheap and share the buffer pool.
    """
    return get_con(db_path).cursor().execute(sql, list(params)).fetchdf()


@st.cache_data(show_spinner=False)
def list_tables(db_path: str) -> set[str]:
    return {r[0] for r in get_con(db_path).cursor().execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
    ).fetchall()}


@st.cache_data(show_spinner=False)
def columns_of(db_path: str, table: str) -> list[str]:
    try:
        return [r[0] for r in get_con(db_path).cursor()
                .execute(f'DESCRIBE "{table}"').fetchall()]
    except Exception:
        return []


def has_col(table: str, col: str) -> bool:
    return col in columns_of(DB_PATH, table)


@st.cache_data(show_spinner=False)
def corpus_facts(db_path: str) -> dict:
    row = q(db_path, """
        SELECT COUNT(*) AS n,
               MIN(report_year) AS y0, MAX(report_year) AS y1,
               MAX(DATE_PREF) AS vintage,
               SUM(CASE WHEN report_year IS NULL OR report_year >= ?
                        THEN 1 ELSE 0 END) AS eligible
        FROM mdr_flat
    """, (CODE_ELIGIBLE_FROM_YEAR,)).iloc[0]
    return {
        "n": int(row["n"]),
        "y0": int(row["y0"]) if pd.notna(row["y0"]) else 1991,
        "y1": int(row["y1"]) if pd.notna(row["y1"]) else 2024,
        "vintage": str(row["vintage"])[:10] if pd.notna(row["vintage"]) else "unknown",
        "eligible": int(row["eligible"]),
    }


def nz(v, kind=int):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return kind(0)
        return kind(v)
    except Exception:
        return kind(0)


def fmt(n) -> str:
    return f"{nz(n):,}"


# ---------------------------------------------------------------------------
# Startup guards
# ---------------------------------------------------------------------------

st.markdown(
    f"""<div style="display:flex;align-items:center;gap:12px;margin-bottom:2px">
      <div style="font-size:26px;font-weight:700;letter-spacing:-.02em">
        Maude<span style="color:#12A594">Dash</span>
        <span style="font-size:13px;font-weight:500;color:#64748B;margin-left:8px">
          Research Edition {VERSION}</span>
      </div>
    </div>""",
    unsafe_allow_html=True,
)

if not os.path.exists(DB_PATH):
    st.error(
        f"**Database not found:** `{DB_PATH}`\n\n"
        "Point MaudeDash at your build with either:\n\n"
        "```bash\n"
        "streamlit run maudedash_app.py -- --db /path/to/maude_final.duckdb\n"
        "```\n\n"
        "or by setting the `MAUDEDASH_DB` environment variable.\n\n"
        f"To build the database from the FDA source files, see `{REPO_URL}`."
    )
    st.stop()

TABLES = list_tables(DB_PATH)
if "mdr_flat" not in TABLES:
    st.error(
        "**This database has no `mdr_flat` table**, so the analytic build has not "
        "completed. Run:\n\n```bash\npython maude_build.py --raw-dir . "
        f"--db {os.path.basename(DB_PATH)} --skip-fts\n```"
    )
    st.stop()

CLINICAL_READY = has_col("mdr_flat", "any_serious_outcome")
HAS_FOI = "foi" in TABLES
HAS_DEV = "flat_dev_problems" in TABLES and "agg_dev_problems_global" in TABLES
HAS_PAT = "flat_pat_problems" in TABLES and "agg_pat_problems_global" in TABLES

FACTS = corpus_facts(DB_PATH)

st.caption(
    f"{fmt(FACTS['n'])} reports · {FACTS['y0']}–{FACTS['y1']} · "
    f"FDA data current to **{FACTS['vintage']}** · "
    f"[Paper]({PAPER_URL}) · [Source]({REPO_URL}) · [Web tool]({SITE_URL})"
)

if not CLINICAL_READY:
    st.warning(
        "`mdr_flat` has no clinical-outcome columns, so the Clinical outcomes, "
        "Subgroup and Sensitivity analyses are unavailable. Rebuild with "
        "`maude_analytic_build_v2.py` to enable them."
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(f"### {APP_NAME}")
    st.caption(APP_TAGLINE)

    st.markdown("#### Define a cohort")
    product_code = st.text_input("FDA product code", "", max_chars=12,
                                 help="Three-letter FDA classification code, e.g. KWP. "
                                      "The most reliable way to isolate a device class.").strip()
    manufacturer = st.text_input("Manufacturer contains", "").strip()
    device_terms_raw = st.text_input("Brand / generic / model contains", "",
                                     help="Semicolon-separated; any match qualifies.").strip()
    narrative = st.text_input("Event narrative contains", "",
                              help="Scans the description text. Slow on wide cohorts.").strip()
    mdr_key = st.text_input("Direct MDR key lookup", "",
                            help="Overrides every other filter.").strip()

    year_range = st.slider("Report year range", FACTS["y0"], FACTS["y1"],
                           (max(FACTS["y0"], CODE_ELIGIBLE_FROM_YEAR), FACTS["y1"]))

    if year_range[0] < CODE_ELIGIBLE_FROM_YEAR:
        st.warning(
            f"Reports before {CODE_ELIGIBLE_FROM_YEAR} carry **no device data, "
            "narratives or problem codes** in this build. Product-code, "
            "manufacturer, brand and narrative filters exclude them entirely.",
            icon="⚠️",
        )

    st.markdown("#### Event type")
    event_picks = [c for c, label in (("D", "Deaths"), ("IN", "Injuries"),
                                      ("M", "Malfunctions"))
                   if st.checkbox(label, value=True, key=f"et_{c}")]

    st.markdown("#### Cohort exclusions")
    exclude_forwarded = st.checkbox("Exclude 803.22(b)(2) forwarded reports")
    exclude_rwd = st.checkbox("Exclude RWD-sourced reports")
    initial_only = st.checkbox("Initial reports only (drop supplements)",
                               help="Recommended for population estimates: "
                                    "deduplicates supplements extending an existing report.")

    if CLINICAL_READY:
        st.markdown("#### Clinical subset")
        implant_only = st.checkbox("Implants only")
        serious_only = st.checkbox("Serious outcomes only (D/L/H/S/C/R)")
    else:
        implant_only = serious_only = False

    st.markdown("---")
    row_cap = st.number_input("Table / export row cap", 100, 500_000, 2_000, 500)

    st.markdown("---")
    st.caption(
        f"MaudeDash {VERSION} · data to {FACTS['vintage']}  \n"
        "Independent research tool; not affiliated with or endorsed by the U.S. FDA."
    )

device_terms = [t.strip() for t in device_terms_raw.split(";") if t.strip()]


# ---------------------------------------------------------------------------
# Cohort SQL
# ---------------------------------------------------------------------------

def build_where(extra: Optional[dict] = None) -> tuple[str, list[Any]]:
    ex = {
        "exclude_forwarded": exclude_forwarded, "exclude_rwd": exclude_rwd,
        "initial_only": initial_only, "implant_only": implant_only,
        "serious_only": serious_only, **(extra or {}),
    }
    if mdr_key:
        return "MDR_REPORT_KEY = ?", [mdr_key]

    parts = ["report_year BETWEEN ? AND ?"]
    params: list[Any] = [year_range[0], year_range[1]]

    if product_code:
        parts.append("product_code = ?")
        params.append(product_code.upper())
    if manufacturer:
        parts.append("manufacturer_l LIKE ?")
        params.append(f"%{manufacturer.lower()}%")
    if device_terms:
        subs = []
        for t in device_terms:
            subs.append("(brand_name_l LIKE ? OR generic_name_l LIKE ? OR model_number_l LIKE ?)")
            params.extend([f"%{t.lower()}%"] * 3)
        parts.append("(" + " OR ".join(subs) + ")")
    if narrative:
        parts.append("narrative_desc_l LIKE ?")
        params.append(f"%{narrative.lower()}%")
    if event_picks and len(event_picks) < 3:
        parts.append(f"EVENT_TYPE IN ({','.join('?' * len(event_picks))})")
        params.extend(event_picks)
    if ex["exclude_forwarded"]:
        parts.append("COALESCE(IS_FORWARDED_803_22_B2, FALSE) = FALSE")
    if ex["exclude_rwd"]:
        parts.append("COALESCE(IS_RWD_SOURCED, FALSE) = FALSE")
    if ex["initial_only"] and has_col("mdr_flat", "initial_report"):
        parts.append("COALESCE(initial_report, TRUE) = TRUE")
    if ex["implant_only"] and has_col("mdr_flat", "implant_flag"):
        parts.append("COALESCE(implant_flag, FALSE) = TRUE")
    if ex["serious_only"] and CLINICAL_READY:
        parts.append("COALESCE(any_serious_outcome, FALSE) = TRUE")

    return " AND ".join(parts), params


WHERE, PARAMS = build_where()
PARAMS_T = tuple(PARAMS)

HAS_COHORT = bool(product_code or manufacturer or device_terms or narrative or mdr_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def label_events(df: pd.DataFrame, col: str = "EVENT_TYPE") -> pd.DataFrame:
    if col in df.columns:
        df = df.copy()
        df[col] = df[col].map(EVENT_LABELS).fillna(df[col])
    return df


def pretty_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: COLUMN_LABELS.get(c, c.replace("_", " ").title())
                              for c in df.columns})


def show_table(df: pd.DataFrame, height: int = 460) -> None:
    st.dataframe(pretty_columns(label_events(df)), width="stretch",
                 height=height, hide_index=True)


def df_to_excel(df: pd.DataFrame, sheet: str = "Sheet1") -> bytes:
    """Excel export with the 32,767-character cell limit handled explicitly."""
    LIMIT = 32_000
    df = df.copy()
    for col in df.select_dtypes(include="object").columns:
        s = df[col].astype(str)
        long = s.str.len() > LIMIT
        if long.any():
            df[col] = s.where(~long, s.str[:LIMIT] + "  …[TRUNCATED]")
    buf = BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name=(sheet[:31] or "Sheet1"))
    except Exception:
        buf = BytesIO()
        buf.write(df.to_csv(index=False).encode())
    return buf.getvalue()


def download_row(df: pd.DataFrame, base: str) -> None:
    """Build an export only when the user asks. v4 serialised every tab's
    DataFrame to XLSX in the sidebar on every rerun."""
    if df is None or df.empty:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    c1, c2, _ = st.columns([1, 1, 4])
    c1.download_button("Download CSV", df.to_csv(index=False).encode(),
                       f"maudedash_{base}_{ts}.csv", "text/csv",
                       key=f"csv_{base}")
    if len(df) <= 200_000:
        c2.download_button("Download Excel", df_to_excel(df, base),
                           f"maudedash_{base}_{ts}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key=f"xlsx_{base}")


def style_fig(fig, height: int = 400, **kw):
    fig.update_layout(template=PLOT_TEMPLATE, height=height,
                      margin=dict(l=60, r=24, t=44, b=48),
                      legend=dict(orientation="h", y=-0.2), **kw)
    return fig


def caveat(text: str = PASSIVE_CAVEAT) -> None:
    st.caption(f"⚠️ {text}")


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

if not HAS_COHORT:
    st.info(
        "**Define a cohort in the sidebar to begin** — a product code, a "
        "manufacturer, a device name, or a phrase from the narrative. "
        "Every analysis recomputes against it.",
        icon="🔍",
    )
    yearly = q(DB_PATH, """
        SELECT report_year AS year, COUNT(*) AS reports,
               SUM(CASE WHEN EVENT_TYPE='D'  THEN 1 ELSE 0 END) AS deaths,
               SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS injuries,
               SUM(CASE WHEN EVENT_TYPE='M'  THEN 1 ELSE 0 END) AS malfunctions
        FROM mdr_flat WHERE report_year IS NOT NULL GROUP BY 1 ORDER BY 1
    """)
    c = st.columns(4)
    c[0].metric("Reports", fmt(FACTS["n"]))
    c[1].metric("Years covered", f"{FACTS['y0']}–{FACTS['y1']}")
    c[2].metric("Code-eligible reports", fmt(FACTS["eligible"]),
                help=f"Reports from {CODE_ELIGIBLE_FROM_YEAR} onward, which are the "
                     "only ones that can carry device or problem-code data.")
    c[3].metric("Data vintage", FACTS["vintage"])

    long = yearly.melt(id_vars="year", value_vars=["malfunctions", "injuries", "deaths"],
                       var_name="Event", value_name="Reports")
    long["Event"] = long["Event"].str.capitalize()
    fig = px.bar(long, x="year", y="Reports", color="Event",
                 title="Reports received per year, whole corpus",
                 color_discrete_map={"Malfunctions": "#1D6A96", "Injuries": "#D9740B",
                                     "Deaths": "#8B1A1A"},
                 labels={"year": "Report year"})
    st.plotly_chart(style_fig(fig, 380, barmode="stack"), use_container_width=True)
    caveat(
        PASSIVE_CAVEAT + " Device-level fields and narratives are unavailable "
        f"for reports before {CODE_ELIGIBLE_FROM_YEAR} in this build."
    )
    st.stop()


# ---------------------------------------------------------------------------
# KPI band
# ---------------------------------------------------------------------------

outcome_sql = (
    ", SUM(CASE WHEN outcome_death THEN 1 ELSE 0 END) AS n_death,"
    " SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious"
    if CLINICAL_READY else ", NULL AS n_death, NULL AS n_serious"
)
kpi = q(DB_PATH, f"""
    SELECT COUNT(*) AS n,
           COUNT(DISTINCT REPORT_NUMBER) AS n_events,
           SUM(CASE WHEN EVENT_TYPE='D'  THEN 1 ELSE 0 END) AS n_d,
           SUM(CASE WHEN EVENT_TYPE='IN' THEN 1 ELSE 0 END) AS n_i,
           SUM(CASE WHEN EVENT_TYPE='M'  THEN 1 ELSE 0 END) AS n_m
           {outcome_sql}
    FROM mdr_flat WHERE {WHERE}
""", PARAMS_T).iloc[0]

N_TOTAL = nz(kpi["n"])

cols = st.columns(8)
cols[0].metric("Reports", fmt(N_TOTAL), help="Submissions, including supplements")
cols[1].metric("Unique events", fmt(kpi["n_events"]), help="Distinct REPORT_NUMBER")
cols[2].metric("Deaths", fmt(kpi["n_d"]), help="EVENT_TYPE = D")
cols[3].metric("Injuries", fmt(kpi["n_i"]))
cols[4].metric("Malfunctions", fmt(kpi["n_m"]))
if CLINICAL_READY:
    n_serious = nz(kpi["n_serious"])
    cols[5].metric("Death outcome", fmt(kpi["n_death"]), help="FDA outcome code D")
    cols[6].metric("Any serious", fmt(n_serious), help="D/L/H/S/C/R per 21 CFR 803.3")
    if N_TOTAL:
        ci = ms.wilson_ci(n_serious, N_TOTAL)
        cols[7].metric("% serious", f"{ci.p * 100:.1f}%",
                       help=f"Wilson 95% CI {ci.lo * 100:.1f}–{ci.hi * 100:.1f}%")

if N_TOTAL == 0:
    st.warning(
        "**No reports match this cohort.** Widen the year range, or check the "
        "product code — MAUDE codes are three letters such as `KWP`. Device "
        f"names and narratives exist only for reports from {CODE_ELIGIBLE_FROM_YEAR} onward.",
        icon="🚫",
    )
    st.stop()


# ---------------------------------------------------------------------------
# Analysis selector — only the chosen analysis runs
# ---------------------------------------------------------------------------

ANALYSES = {
    "Overview": ["Reports", "Yearly volume", "Event types"],
    "Clinical": ["Clinical outcomes", "Demographics", "Device age", "Deaths"],
    "Signals": ["Problem codes", "Disproportionality"],
    "Inference": ["Subgroups", "Trend tests", "Compare cohorts", "Sensitivity"],
    "Context": ["Reporter & source", "Geography", "Manufacturers", "Reporting lag"],
    "Narrative": ["Text mining", "Narrative text"],
    "Report": ["Data quality", "Methods & STROBE", "Export"],
}
FLAT = [a for group in ANALYSES.values() for a in group]

nav1, nav2 = st.columns([1, 3])
group = nav1.selectbox("Section", list(ANALYSES.keys()))
analysis = nav2.radio("Analysis", ANALYSES[group], horizontal=True,
                      label_visibility="visible")
st.markdown("---")


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

if analysis == "Reports":
    st.subheader("Matching reports")
    cols_show = ["MDR_REPORT_KEY", "REPORT_NUMBER", "DATE_PREF", "EVENT_TYPE",
                 "manufacturer", "BRAND_NAME", "GENERIC_NAME", "MODEL_NUMBER",
                 "product_code", "device_count", "patient_count"]
    if CLINICAL_READY:
        cols_show += ["any_serious_outcome", "outcome_codes_raw", "implant_flag"]
    df = q(DB_PATH, f"""
        SELECT {', '.join(cols_show)} FROM mdr_flat WHERE {WHERE}
        ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY LIMIT {int(row_cap)}
    """, PARAMS_T)
    st.caption(f"Showing {fmt(len(df))} of {fmt(N_TOTAL)} reports"
               + (" — raise the row cap in the sidebar to see more."
                  if len(df) >= row_cap else "."))
    show_table(df, 520)
    download_row(df, "reports")

elif analysis == "Yearly volume":
    st.subheader("Reports per year")
    df = q(DB_PATH, f"""
        SELECT report_year AS year, COUNT(*) AS reports FROM mdr_flat
        WHERE {WHERE} AND report_year IS NOT NULL GROUP BY 1 ORDER BY 1
    """, PARAMS_T)
    if df.empty:
        st.info("No report in this cohort carries a usable date.")
    else:
        fig = px.bar(df, x="year", y="reports",
                     labels={"year": "Report year", "reports": "Reports"},
                     title=f"Reports per year (n = {fmt(N_TOTAL)})")
        fig.update_traces(marker_color="#1D6A96")
        st.plotly_chart(style_fig(fig), use_container_width=True)
        if len(df) >= 3 and df["reports"].iloc[-1] < df["reports"].iloc[-2] * 0.75:
            st.warning(
                f"**{int(df['year'].iloc[-1])} looks incomplete.** The FDA files were "
                "captured mid-cycle, so the final year is usually partial. Exclude it "
                "from rate comparisons and trend tests.", icon="⚠️")
        caveat("Report volume tracks reporting behaviour as much as device "
               "performance: regulatory changes, recalls and publicity all move "
               "these curves independently of real-world risk.")
        download_row(df, "yearly_volume")

elif analysis == "Event types":
    st.subheader("Event types over time")
    df = q(DB_PATH, f"""
        SELECT report_year AS year, EVENT_TYPE, COUNT(*) AS reports FROM mdr_flat
        WHERE {WHERE} AND report_year IS NOT NULL AND EVENT_TYPE IN ('D','IN','M')
        GROUP BY 1,2 ORDER BY 1,2
    """, PARAMS_T)
    if df.empty:
        st.info("No report carries a death / injury / malfunction classification.")
    else:
        df["Event"] = df["EVENT_TYPE"].map(EVENT_LABELS).fillna(df["EVENT_TYPE"])
        fig = px.line(df, x="year", y="reports", color="Event", markers=True,
                      labels={"year": "Report year", "reports": "Reports"},
                      title=f"Event type by year (n = {fmt(N_TOTAL)})",
                      color_discrete_map={"Death": "#8B1A1A", "Injury": "#D9740B",
                                          "Malfunction": "#1D6A96"})
        st.plotly_chart(style_fig(fig), use_container_width=True)
        caveat()
        download_row(df, "event_trends")

elif analysis == "Clinical outcomes":
    st.subheader("Clinical outcomes (21 CFR 803.3)")
    st.caption("Patient outcome codes from `SEQUENCE_NUMBER_OUTCOME` — the FDA's own "
               "categories, more precise than EVENT_TYPE. A report may carry several.")
    if not CLINICAL_READY:
        st.info("Rebuild with the v2 analytic build to enable this analysis.")
    else:
        sums = ", ".join(f"SUM(CASE WHEN {c} THEN 1 ELSE 0 END) AS {c}"
                         for c, _, _ in OUTCOMES)
        r = q(DB_PATH, f"SELECT COUNT(*) AS n, {sums} FROM mdr_flat WHERE {WHERE}",
              PARAMS_T).iloc[0]
        n = nz(r["n"])
        rows = []
        for col, label, colour in OUTCOMES:
            k = nz(r[col])
            ci = ms.wilson_ci(k, n)
            rows.append({"Outcome": label, "n": k, "Total": n, "colour": colour,
                         "Rate": ci.p * 100, "lo": ci.lo * 100, "hi": ci.hi * 100,
                         "Rate (95% CI)": ci.as_str()})
        out = pd.DataFrame(rows)

        fig = go.Figure()
        for _, row in out.iterrows():
            fig.add_trace(go.Bar(
                y=[row["Outcome"]], x=[row["Rate"]], orientation="h",
                marker_color=row["colour"], showlegend=False,
                error_x=dict(type="data", symmetric=False,
                             array=[row["hi"] - row["Rate"]],
                             arrayminus=[row["Rate"] - row["lo"]],
                             color="rgba(0,0,0,.45)", thickness=1.3, width=4),
                hovertemplate=(f"{row['Outcome']}<br>{fmt(row['n'])} of {fmt(n)}"
                               f"<br>{row['Rate (95% CI)']}<extra></extra>"),
            ))
        # Scale to the data. v4 pinned this axis to 0-100%, which renders
        # sub-1% harms — most of them — as invisible slivers.
        axis_max = min(100.0, max(float(out["hi"].max()) * 1.15, 0.5))
        style_fig(fig, 440, margin=dict(l=190, r=30, t=46, b=48))
        fig.update_layout(
            title=f"Outcome rates with Wilson 95% CIs (n = {fmt(n)} reports)",
            xaxis=dict(title="Rate (% of reports)", range=[0, axis_max]))
        st.plotly_chart(fig, use_container_width=True)
        show_table(out[["Outcome", "n", "Total", "Rate (95% CI)"]], 330)
        caveat(PASSIVE_CAVEAT + " Outcome coding is voluntary — reports without an "
               "outcome code are not necessarily benign. Check Data quality for coverage.")
        download_row(out.drop(columns=["colour"]), "clinical_outcomes")

elif analysis == "Demographics":
    st.subheader("Patient demographics")
    df = q(DB_PATH, f"""
        SELECT age_years_avg AS age, sex_list FROM mdr_flat
        WHERE {WHERE} AND age_years_avg IS NOT NULL
          AND age_years_avg BETWEEN 0 AND 110 LIMIT 400000
    """, PARAMS_T)
    sexes = q(DB_PATH, f"""
        SELECT upper(left(coalesce(sex_list,'U'),1)) AS sex, COUNT(*) AS n
        FROM mdr_flat WHERE {WHERE} GROUP BY 1 ORDER BY n DESC
    """, PARAMS_T)
    c1, c2 = st.columns(2)
    with c1:
        if df.empty:
            st.info("No patient age reported for this cohort.")
        else:
            fig = px.histogram(df, x="age", nbins=24,
                               labels={"age": "Age at event (years)"},
                               title=f"Patient age (n = {fmt(len(df))} with age)")
            fig.update_traces(marker_color="#1D6A96")
            st.plotly_chart(style_fig(fig), use_container_width=True)
            st.caption(
                f"Age is present for {len(df) / N_TOTAL:.1%} of the cohort and is the "
                "**mean across patients on a report**, not an individual patient age.")
    with c2:
        named = {"F": "Female", "M": "Male"}
        sexes["Sex"] = sexes["sex"].map(named).fillna("Unknown / not reported")
        agg = sexes.groupby("Sex", as_index=False)["n"].sum()
        fig = px.pie(agg, names="Sex", values="n", hole=0.4, title="Patient sex",
                     color_discrete_sequence=["#1D6A96", "#12A594", "#94A3B8"])
        st.plotly_chart(style_fig(fig), use_container_width=True)
        st.caption("Sex is taken from the first character of a multi-patient list, "
                   "so multi-patient reports are represented by their first patient.")
        download_row(agg, "sex_distribution")

elif analysis == "Device age":
    st.subheader("Device age at failure")
    if not has_col("mdr_flat", "device_age_days"):
        st.info("`device_age_days` is not present in this build.")
    else:
        df = q(DB_PATH, f"""
            SELECT device_age_days / 365.25 AS age_years FROM mdr_flat
            WHERE {WHERE} AND device_age_days IS NOT NULL
              AND device_age_days BETWEEN 0 AND 36525 LIMIT 300000
        """, PARAMS_T)
        if df.empty:
            st.info("No device-age data in this cohort. MAUDE's DEVICE_AGE_TEXT is "
                    f"often unpopulated and absent entirely before {CODE_ELIGIBLE_FROM_YEAR}.")
        else:
            c1, c2 = st.columns(2)
            with c1:
                fig = px.histogram(df, x="age_years", nbins=40,
                                   labels={"age_years": "Device age (years)"},
                                   title=f"Age at failure (n = {fmt(len(df))})")
                fig.update_traces(marker_color="#1D6A96")
                st.plotly_chart(style_fig(fig), use_container_width=True)
            with c2:
                s = df["age_years"].sort_values().reset_index(drop=True)
                cum = pd.DataFrame({"age_years": s,
                                    "cumulative": (s.index + 1) / len(s) * 100})
                fig = px.line(cum, x="age_years", y="cumulative",
                              labels={"age_years": "Device age (years)",
                                      "cumulative": "Cumulative % of failures"},
                              title="Cumulative distribution")
                fig.update_traces(line_color="#12A594")
                st.plotly_chart(style_fig(fig), use_container_width=True)
            d = df["age_years"].describe()
            st.dataframe(pd.DataFrame([{
                "Reports with age": len(df),
                "Median (y)": round(d["50%"], 2),
                "IQR (y)": f"{d['25%']:.2f}–{d['75%']:.2f}",
                "Mean (y)": round(d["mean"], 2),
                "P90 (y)": round(df['age_years'].quantile(.9), 2),
            }]), width="stretch", hide_index=True)
            st.caption(f"Device age is reported for {len(df) / N_TOTAL:.1%} of this cohort; "
                       "the distribution describes only those reports.")
            download_row(df, "device_age")

elif analysis == "Deaths":
    st.subheader("Death reports")
    clause = ("(EVENT_TYPE = 'D' OR COALESCE(outcome_death, FALSE) = TRUE)"
              if CLINICAL_READY else "EVENT_TYPE = 'D'")
    # A true count. v4 displayed len(df) after LIMIT 5000, so any cohort with
    # more than 5,000 deaths silently reported exactly "5,000".
    n_death = nz(q(DB_PATH, f"SELECT COUNT(*) AS n FROM mdr_flat WHERE {WHERE} AND {clause}",
                   PARAMS_T).iloc[0]["n"])
    if n_death == 0:
        st.info("No report in this cohort is classified as a death.")
    else:
        st.metric("Death reports", fmt(n_death))
        cols_show = ["MDR_REPORT_KEY", "DATE_PREF", "manufacturer", "BRAND_NAME",
                     "GENERIC_NAME", "product_code", "narrative_desc"]
        df = q(DB_PATH, f"""
            SELECT {', '.join(cols_show)} FROM mdr_flat
            WHERE {WHERE} AND {clause}
            ORDER BY DATE_PREF DESC NULLS LAST LIMIT {int(row_cap)}
        """, PARAMS_T)
        st.caption(f"Showing {fmt(len(df))} of {fmt(n_death)}.")
        show_table(df, 520)
        caveat("A death report records that a death occurred and a device was "
               "involved. It is **not** an FDA determination that the device "
               "caused the death.")
        download_row(df, "deaths")

elif analysis == "Problem codes":
    st.subheader("Coded device and patient problems")
    c1, c2 = st.columns(2)
    for col, (ok, bridge, dic, title, colour) in zip(
        (c1, c2),
        ((HAS_PAT, "flat_pat_problems", "patient_problem_dict", "Patient problems", "#12A594"),
         (HAS_DEV, "flat_dev_problems", "device_problem_dict", "Device problems", "#1D6A96")),
    ):
        with col:
            st.markdown(f"**{title}**")
            if not ok:
                st.info(f"`{bridge}` is not present in this build.")
                continue
            df = q(DB_PATH, f"""
                WITH keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {WHERE})
                SELECT COALESCE(d.TERM, b.code) AS Term, b.code AS Code,
                       COUNT(DISTINCT b.MDR_REPORT_KEY) AS n
                FROM {bridge} b JOIN keys USING (MDR_REPORT_KEY)
                LEFT JOIN {dic} d ON TRIM(d.FDA_CODE) = b.code
                GROUP BY 1,2 ORDER BY n DESC LIMIT 50
            """, PARAMS_T)
            if df.empty:
                st.info("No coded problems for this cohort.")
                continue
            top = df.head(20).sort_values("n")
            fig = px.bar(top, y="Term", x="n", orientation="h",
                         labels={"n": "Reports", "Term": ""},
                         title=f"Top 20 of {len(df)} codes")
            fig.update_traces(marker_color=colour)
            st.plotly_chart(style_fig(fig, 500, margin=dict(l=230, r=20, t=44, b=44)),
                            use_container_width=True)
            download_row(df, f"{title.split()[0].lower()}_problems")

elif analysis == "Disproportionality":
    st.subheader("Disproportionality analysis (PRR & ROR)")
    which = st.radio("Compute against", ["Device problems", "Patient problems"],
                     horizontal=True)
    is_dev = which == "Device problems"
    ok = HAS_DEV if is_dev else HAS_PAT
    bridge = "flat_dev_problems" if is_dev else "flat_pat_problems"
    glob = "agg_dev_problems_global" if is_dev else "agg_pat_problems_global"
    dic = "device_problem_dict" if is_dev else "patient_problem_dict"

    if not ok:
        st.info(f"`{bridge}` / `{glob}` are not present in this build.")
    else:
        eligible = FACTS["eligible"]
        elig_clause = f"(report_year IS NULL OR report_year >= {CODE_ELIGIBLE_FROM_YEAR})"
        cohort_n = nz(q(DB_PATH,
            f"SELECT COUNT(*) AS n FROM mdr_flat WHERE {WHERE} AND {elig_clause}",
            PARAMS_T).iloc[0]["n"])

        if cohort_n < 3:
            st.info("Disproportionality needs at least three code-eligible reports "
                    f"({CODE_ELIGIBLE_FROM_YEAR} onward) in the cohort.")
        else:
            df = q(DB_PATH, f"""
                WITH keys AS (
                    SELECT MDR_REPORT_KEY FROM mdr_flat
                    WHERE {WHERE} AND {elig_clause}
                ),
                inside AS (
                    SELECT b.code, COUNT(DISTINCT b.MDR_REPORT_KEY) AS a
                    FROM {bridge} b JOIN keys USING (MDR_REPORT_KEY)
                    GROUP BY 1 HAVING a >= 3
                )
                SELECT COALESCE(d.TERM, i.code) AS Term, i.code AS Code,
                       i.a AS a, g.n AS global_n
                FROM inside i JOIN {glob} g USING (code)
                LEFT JOIN {dic} d ON TRIM(d.FDA_CODE) = i.code
            """, PARAMS_T)
            if df.empty:
                st.info("No problem code reaches three reports in this cohort.")
            else:
                recs = []
                for _, r in df.iterrows():
                    a = int(r["a"])
                    b = cohort_n - a
                    c = int(r["global_n"]) - a
                    d = eligible - int(r["global_n"]) - cohort_n + a
                    res = ms.analyze_2x2(a, b, c, d)
                    ic = ms.information_component(a, b, c, d)
                    # Prefer Fisher where computed (small cells), else Yates chi2.
                    p_used = res.fisher_p
                    if p_used is None and not math.isnan(res.chi2_yates):
                        p_used = ms.chi2_sf(res.chi2_yates, 1)
                    recs.append({
                        "Term": r["Term"], "Code": r["Code"], "a": a,
                        "res": res, "ic": ic, "p": p_used,
                        "ema": ms.ema_signal(a, res.prr.point, res.chi2_yates),
                    })

                # Screening hundreds of codes at alpha=0.05 yields dozens of
                # spurious hits; v4 applied no multiplicity control at all.
                qs = ms.benjamini_hochberg([x["p"] for x in recs])
                for x, qv in zip(recs, qs):
                    x["q"] = qv

                n_ema = sum(1 for x in recs if x["ema"])
                n_ic = sum(1 for x in recs if x["ic"].signal)
                n_both = sum(1 for x in recs if x["ema"] and x["ic"].signal)
                n_q = sum(1 for x in recs if x["q"] is not None and x["q"] < 0.05)

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("EMA-2008 signals", fmt(n_ema),
                          help="PRR ≥ 2, χ² ≥ 4, at least 3 reports")
                k2.metric("IC025 > 0 (WHO)", fmt(n_ic),
                          help="Bayesian Information Component lower bound above "
                               "zero — stable for rare codes, unlike PRR")
                k3.metric("Both criteria", fmt(n_both),
                          help="Flagged by both the frequentist and Bayesian rules "
                               "— the defensible set")
                k4.metric("FDR q < 0.05", fmt(n_q),
                          help="Benjamini-Hochberg adjusted across every code tested")

                out = pd.DataFrame([{
                    "Term": x["Term"], "Code": x["Code"], "a": x["a"],
                    "Expected": round(x["ic"].expected, 2),
                    "PRR (95% CI)": x["res"].prr.as_str(),
                    "ROR (95% CI)": x["res"].ror.as_str(),
                    "IC (95% CrI)": x["ic"].as_str(),
                    "χ² (Yates)": (round(x["res"].chi2_yates, 2)
                                   if not math.isnan(x["res"].chi2_yates) else None),
                    "FDR q": (f"{x['q']:.3g}" if x["q"] is not None else "—"),
                    "Signal": " + ".join(
                        [s for s in (("EMA" if x["ema"] else None),
                                     ("IC" if x["ic"].signal else None)) if s]) or "—",
                    "_sort": (x["ic"].ic025 if not math.isnan(x["ic"].ic025) else -99),
                } for x in recs]).sort_values("_sort", ascending=False).drop(columns=["_sort"])

                st.caption(
                    f"{len(out)} codes with at least 3 reports in the cohort, "
                    "ranked by IC025 — the most conservative measure, so the least "
                    "fragile signals appear first. Click any column header to re-sort.")
                st.dataframe(out, width="stretch", height=520, hide_index=True)

                st.info(
                    "**Reading this table.** *a* is the observed report count and "
                    "*Expected* what independence predicts. **PRR** and **ROR** are "
                    "frequentist ratios that become unstable for rare codes. **IC** is "
                    "the WHO-UMC Bayesian measure with shrinkage; a code signals when "
                    "its lower credibility bound **IC025 > 0**, which a single stray "
                    "report can never achieve. **FDR q** is the Benjamini-Hochberg "
                    f"adjusted p-value across all {len(out)} codes tested — an "
                    f"unadjusted p of 0.04 among {len(out)} tests means nothing. Codes "
                    "flagged by **both** rules are the defensible set.", icon="ℹ️")

                st.info(
                    f"**Comparator:** {fmt(eligible)} reports eligible to carry a "
                    f"problem code ({CODE_ELIGIBLE_FROM_YEAR} onward), not the full "
                    f"{fmt(FACTS['n'])}-report corpus. The FDA problem-code files begin "
                    f"in {CODE_ELIGIBLE_FROM_YEAR}; including earlier reports — as "
                    "MaudeDash v4 did — inflates every PRR by roughly 25% and "
                    "manufactures signals at the PRR ≥ 2 threshold.", icon="ℹ️")
                caveat(
                    "A signal is **hypothesis-generating, not causal**. " + PASSIVE_CAVEAT +
                    " Disproportionality is vulnerable to stimulated reporting after "
                    "publicity, differential reporting between manufacturers, and "
                    "indication bias. No multiplicity adjustment is applied across the "
                    f"{len(out)} codes tested.")
                download_row(out, "disproportionality")

elif analysis == "Subgroups":
    st.subheader("Subgroup analysis")
    if not CLINICAL_READY:
        st.info("Rebuild with the v2 analytic build to enable this analysis.")
    else:
        c1, c2 = st.columns(2)
        outcome = c1.selectbox("Outcome", [c for c, _, _ in OUTCOMES],
                               format_func=lambda c: dict((a, b) for a, b, _ in OUTCOMES)[c])
        strat_map = {
            "Sex": "CASE upper(left(coalesce(sex_list,'U'),1)) WHEN 'F' THEN 'Female' "
                   "WHEN 'M' THEN 'Male' ELSE 'Unknown' END",
            "Age band": AGE_BAND_SQL,
            "Year": "report_year::VARCHAR",
            "Source type": "upper(trim(coalesce(SOURCE_TYPE,'Unknown')))",
            "Country": "upper(trim(coalesce(reporter_country_code,'Unknown')))",
            "Reporter occupation": "upper(trim(coalesce(REPORTER_OCCUPATION_CODE,'UNK')))",
        }
        by = c2.selectbox("Stratify by", list(strat_map))

        total_groups = nz(q(DB_PATH, f"""
            SELECT COUNT(*) AS n FROM (
              SELECT {strat_map[by]} AS g FROM mdr_flat WHERE {WHERE} GROUP BY 1)
        """, PARAMS_T).iloc[0]["n"])

        df = q(DB_PATH, f"""
            SELECT {strat_map[by]} AS subgroup, COUNT(*) AS n,
                   SUM(CASE WHEN {outcome} THEN 1 ELSE 0 END) AS k
            FROM mdr_flat WHERE {WHERE} GROUP BY 1 ORDER BY n DESC LIMIT 25
        """, PARAMS_T)
        if df.empty:
            st.info("This stratification produced no groups.")
        else:
            if by == "Reporter occupation":
                df["subgroup"] = df["subgroup"].map(
                    lambda c: OCCUPATION.get(str(c), str(c)))
            df["n"] = df["n"].astype(int)
            df["k"] = df["k"].astype(int)
            cis = [ms.wilson_ci(int(r.k), int(r.n)) for r in df.itertuples()]
            df["rate"] = [c.p * 100 for c in cis]
            df["lo"] = [c.lo * 100 for c in cis]
            df["hi"] = [c.hi * 100 for c in cis]

            table = [[int(r.k), int(r.n) - int(r.k)] for r in df.itertuples()]
            chi2, p, dof = ms.chi2_independence(table)
            min_exp = ms.min_expected_cell(table)

            fig = go.Figure()
            for _, r in df.iloc[::-1].iterrows():
                tag = f"{r['subgroup']} *" if r["n"] < 10 else str(r["subgroup"])
                fig.add_trace(go.Scatter(x=[r["lo"], r["hi"]], y=[tag, tag],
                                         mode="lines", line=dict(color="#2E86B8", width=2),
                                         showlegend=False, hoverinfo="skip"))
                fig.add_trace(go.Scatter(
                    x=[r["rate"]], y=[tag], mode="markers",
                    marker=dict(color="#12507A", size=9), showlegend=False,
                    hovertemplate=(f"{r['subgroup']}<br>{fmt(r['k'])} / {fmt(r['n'])}"
                                   f"<br>{r['rate']:.2f}% "
                                   f"({r['lo']:.2f}–{r['hi']:.2f})<extra></extra>")))
            label = dict((a, b) for a, b, _ in OUTCOMES)[outcome]
            style_fig(fig, max(360, 26 * len(df) + 100),
                      margin=dict(l=200, r=30, t=46, b=48))
            fig.update_layout(title=f"{label} by {by} — Wilson 95% CIs",
                              xaxis_title=f"{label} rate (% of reports)")
            st.plotly_chart(fig, use_container_width=True)

            msg = f"χ² = {chi2:.2f}, df = {dof}, p = {p:.4g}" if p is not None \
                  else f"χ² = {chi2:.2f}, df = {dof}"
            if total_groups > len(df):
                st.warning(
                    f"**Showing the {len(df)} largest of {total_groups} strata**, and the "
                    "χ² below is computed on those only — it is not a test across all "
                    "strata. v4 applied the same truncation without saying so.", icon="⚠️")
            if min_exp < 5:
                st.warning(f"{msg}. Smallest expected cell is {min_exp:.2f}; below 5 the "
                           "χ² approximation is unreliable (Cochran's rule). Treat this "
                           "as descriptive.", icon="⚠️")
            else:
                st.info(f"{msg}", icon="ℹ️")
            st.caption("Strata marked * have n < 10 and unstable intervals.")

            out = df[["subgroup", "k", "n", "rate", "lo", "hi"]].copy()
            out.columns = ["Subgroup", "Outcome n", "Total", "Rate %", "CI lo %", "CI hi %"]
            show_table(out, 340)
            download_row(out, "subgroup_analysis")

elif analysis == "Trend tests":
    st.subheader("Trend tests on yearly counts")
    targets = [("Total reports", None), ("Deaths (event type)", "EVENT_TYPE = 'D'")]
    if CLINICAL_READY:
        targets += [(f"Outcome: {label}", f"{c} = TRUE") for c, label, _ in OUTCOMES]
    label, clause = st.selectbox("Target", targets, format_func=lambda t: t[0])
    counter = f"SUM(CASE WHEN {clause} THEN 1 ELSE 0 END)" if clause else "COUNT(*)"

    df = q(DB_PATH, f"""
        SELECT report_year AS year, COUNT(*) AS n, {counter} AS k
        FROM mdr_flat WHERE {WHERE} AND report_year IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """, PARAMS_T)
    if len(df) < 2:
        st.info("Trend testing needs at least two years of data.")
    else:
        df["n"] = df["n"].astype(int)
        df["k"] = df["k"].astype(int)
        ca = ms.cochran_armitage_trend(df["k"].tolist(), df["n"].tolist(),
                                       df["year"].tolist())
        mk = ms.mann_kendall(df["k"].tolist())
        c1, c2 = st.columns(2)
        c1.metric("Cochran-Armitage", f"z = {ca.statistic:.3f}",
                  delta=ca.direction, delta_color="off")
        c1.caption(f"Trend in proportion · p = "
                   f"{ca.p_value:.4g}" if ca.p_value is not None else "p = n/a")
        c2.metric("Mann-Kendall", f"S = {int(mk.statistic)}",
                  delta=mk.direction, delta_color="off")
        c2.caption(f"Monotonic trend in counts · p = "
                   f"{mk.p_value:.4g}" if mk.p_value is not None else "p = n/a")

        df["rate"] = 100 * df["k"] / df["n"].replace(0, pd.NA)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df["year"], y=df["k"], name="Count",
                             marker_color="#1D6A96"))
        if clause:
            fig.add_trace(go.Scatter(x=df["year"], y=df["rate"], name="Rate (%)",
                                     yaxis="y2", mode="lines+markers",
                                     line=dict(color="#C0392B", width=2.2)))
        style_fig(fig)
        fig.update_layout(title=f"{label} per year",
                          xaxis_title="Report year", yaxis_title="Count",
                          yaxis2=dict(title="Rate (%)", overlaying="y", side="right"))
        st.plotly_chart(fig, use_container_width=True)
        caveat("Significance here describes the reporting series, not device risk. "
               + PASSIVE_CAVEAT + " No multiplicity adjustment is applied if you "
               "test several targets.")
        download_row(df, "trend_tests")

elif analysis == "Compare cohorts":
    st.subheader("Cohort comparison")
    split = st.radio("Split by", ["Year range", "Manufacturer", "Event type"],
                     horizontal=True)
    clause_a = clause_b = None
    pa: list = []
    pb: list = []
    la = lb = ""
    if split == "Year range":
        mid = (year_range[0] + year_range[1]) // 2
        c1, c2 = st.columns(2)
        ay = c1.slider("Cohort A", year_range[0], year_range[1], (year_range[0], mid))
        by_ = c2.slider("Cohort B", year_range[0], year_range[1], (min(mid + 1, year_range[1]), year_range[1]))
        clause_a, pa, la = "report_year BETWEEN ? AND ?", list(ay), f"{ay[0]}–{ay[1]}"
        clause_b, pb, lb = "report_year BETWEEN ? AND ?", list(by_), f"{by_[0]}–{by_[1]}"
    elif split == "Manufacturer":
        c1, c2 = st.columns(2)
        la = c1.text_input("Cohort A manufacturer contains", "").strip()
        lb = c2.text_input("Cohort B manufacturer contains", "").strip()
        if la and lb:
            clause_a, pa = "manufacturer_l LIKE ?", [f"%{la.lower()}%"]
            clause_b, pb = "manufacturer_l LIKE ?", [f"%{lb.lower()}%"]
    else:
        c1, c2 = st.columns(2)
        a = c1.selectbox("Cohort A event type", ["D", "IN", "M"], 0,
                         format_func=lambda c: EVENT_LABELS[c])
        b = c2.selectbox("Cohort B event type", ["D", "IN", "M"], 1,
                         format_func=lambda c: EVENT_LABELS[c])
        if a == b:
            st.info("Choose two different event types.")
        else:
            clause_a, pa, la = "EVENT_TYPE = ?", [a], EVENT_LABELS[a]
            clause_b, pb, lb = "EVENT_TYPE = ?", [b], EVENT_LABELS[b]

    if clause_a and clause_b and la and lb:
        def grab(clause, extra):
            return q(DB_PATH, f"""
                SELECT EVENT_TYPE, COUNT(*) AS n FROM mdr_flat
                WHERE {WHERE} AND {clause} GROUP BY 1
            """, tuple(PARAMS + list(extra)))
        ra, rb = grab(clause_a, pa), grab(clause_b, pb)
        types = ["D", "IN", "M"]
        va = [nz(ra.loc[ra.EVENT_TYPE == t, "n"].sum()) for t in types]
        vb = [nz(rb.loc[rb.EVENT_TYPE == t, "n"].sum()) for t in types]
        if not any(va) and not any(vb):
            st.info("Neither split matched any report.")
        else:
            plot_df = pd.DataFrame({
                "Event": [EVENT_LABELS[t] for t in types] * 2,
                "Reports": va + vb,
                "Cohort": [la] * 3 + [lb] * 3,
            })
            fig = px.bar(plot_df, x="Event", y="Reports", color="Cohort",
                         barmode="group", title=f"{la} vs {lb}",
                         color_discrete_sequence=["#1D6A96", "#12A594"])
            st.plotly_chart(style_fig(fig), use_container_width=True)
            # Sum, don't average. v4 used pivot_table's default aggfunc="mean".
            table = [[va[i], vb[i]] for i in range(3)]
            chi2, p, dof = ms.chi2_independence(table)
            min_exp = ms.min_expected_cell(table)
            sa, sb = max(sum(va), 1), max(sum(vb), 1)
            out = pd.DataFrame({
                "Event type": [EVENT_LABELS[t] for t in types],
                la: va, f"{la} %": [round(v / sa * 100, 2) for v in va],
                lb: vb, f"{lb} %": [round(v / sb * 100, 2) for v in vb],
            })
            show_table(out, 220)
            msg = f"χ² = {chi2:.2f}, df = {dof}" + (f", **p = {p:.4g}**" if p is not None else "")
            if min_exp < 5:
                st.warning(f"{msg}. Smallest expected cell {min_exp:.2f} < 5 — "
                           "treat as descriptive.", icon="⚠️")
            else:
                st.info(msg, icon="ℹ️")
            download_row(out, "cohort_comparison")

elif analysis == "Sensitivity":
    st.subheader("Sensitivity analysis")
    st.caption("The same headline figures under the standard MAUDE exclusion "
               "scenarios, so you can see whether a result depends on them.")
    scenarios = [
        ("Base case (current filters)", {}),
        ("Excluding forwarded reports", {"exclude_forwarded": True}),
        ("Excluding RWD-sourced reports", {"exclude_rwd": True}),
        ("Initial reports only", {"initial_only": True}),
        ("Conservative (all three)", {"exclude_forwarded": True,
                                      "exclude_rwd": True, "initial_only": True}),
    ]
    rows = []
    for label, extra in scenarios:
        w, p_ = build_where(extra)
        extra_sel = (", SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS serious"
                     if CLINICAL_READY else ", NULL AS serious")
        r = q(DB_PATH, f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS deaths
                   {extra_sel}
            FROM mdr_flat WHERE {w}
        """, tuple(p_)).iloc[0]
        row = {"Scenario": label, "Reports": nz(r["n"]),
               "Deaths (event type)": nz(r["deaths"])}
        if CLINICAL_READY:
            ser = nz(r["serious"])
            row["Serious (outcome)"] = ser
            row["% serious (95% CI)"] = (ms.wilson_ci(ser, nz(r["n"])).as_str()
                                         if nz(r["n"]) else "—")
        rows.append(row)
    out = pd.DataFrame(rows)
    show_table(out, 250)
    base, cons = out["Reports"].iloc[0], out["Reports"].iloc[-1]
    st.info(f"The conservative scenario retains **{cons / base:.1%}** of the base "
            f"cohort ({fmt(cons)} of {fmt(base)}). If your headline estimate moves "
            "materially across these rows, report the sensitivity analysis alongside "
            "it rather than the base case alone.", icon="ℹ️")
    download_row(out, "sensitivity")

elif analysis == "Reporter & source":
    st.subheader("Reporter occupation and report source")
    c1, c2 = st.columns(2)
    with c1:
        df = q(DB_PATH, f"""
            SELECT coalesce(nullif(trim(REPORTER_OCCUPATION_CODE),''),'UNK') AS code,
                   COUNT(*) AS n FROM mdr_flat WHERE {WHERE} GROUP BY 1 ORDER BY n DESC
        """, PARAMS_T)
        if not df.empty:
            df["Occupation"] = df["code"].map(
                lambda c: OCCUPATION.get(str(c).upper(), f"Other ({c})"))
            top = df.head(10).copy()
            if len(df) > 10:
                top = pd.concat([top, pd.DataFrame([{
                    "Occupation": f"All other ({len(df) - 10} codes)",
                    "n": int(df["n"].iloc[10:].sum())}])], ignore_index=True)
            fig = px.pie(top, names="Occupation", values="n", hole=0.4,
                         title=f"Reporter occupation (n = {fmt(N_TOTAL)})")
            st.plotly_chart(style_fig(fig), use_container_width=True)
            download_row(df[["Occupation", "code", "n"]], "reporter_occupation")
    with c2:
        df = q(DB_PATH, f"""
            WITH base AS (SELECT SOURCE_TYPE FROM mdr_flat WHERE {WHERE}
                          AND SOURCE_TYPE IS NOT NULL AND trim(SOURCE_TYPE) <> '')
            SELECT upper(trim(s.value)) AS code, COUNT(*) AS n
            FROM base, unnest(string_split(SOURCE_TYPE, ',')) s(value)
            WHERE trim(s.value) <> '' GROUP BY 1 ORDER BY n DESC LIMIT 15
        """, PARAMS_T)
        if not df.empty:
            df["Source"] = df["code"].map(SOURCE_LABELS).fillna(df["code"])
            fig = px.bar(df.sort_values("n"), y="Source", x="n", orientation="h",
                         labels={"n": "Reports", "Source": ""}, title="Report source")
            fig.update_traces(marker_color="#12A594")
            st.plotly_chart(style_fig(fig, 400, margin=dict(l=150, r=20, t=44, b=44)),
                            use_container_width=True)
            download_row(df[["Source", "code", "n"]], "report_source")

elif analysis == "Geography":
    st.subheader("Reporter geography")
    df = q(DB_PATH, f"""
        SELECT upper(trim(reporter_country_code)) AS country, COUNT(*) AS n
        FROM mdr_flat WHERE {WHERE} AND reporter_country_code IS NOT NULL
          AND trim(reporter_country_code) <> '' GROUP BY 1 ORDER BY n DESC
    """, PARAMS_T)
    if df.empty:
        st.info("No report in this cohort records a reporter country.")
    else:
        c1, c2 = st.columns([2, 1])
        with c1:
            try:
                fig = px.choropleth(df, locations="country", color="n",
                                    locationmode="ISO-3", color_continuous_scale="Blues",
                                    labels={"n": "Reports"},
                                    title=f"Reports by country ({len(df)} codes)")
                st.plotly_chart(style_fig(fig, 430), use_container_width=True)
                st.caption("MAUDE records ISO-2 country codes while this map expects "
                           "ISO-3, so only codes valid in both appear. The table lists "
                           "every code.")
            except Exception as exc:
                st.warning(f"The map could not be drawn ({exc}). The table below is "
                           "unaffected.", icon="⚠️")
        with c2:
            show_table(df.head(30), 430)
        download_row(df, "geography")

elif analysis == "Manufacturers":
    st.subheader("Manufacturer concentration")
    df = q(DB_PATH, f"""
        SELECT manufacturer, COUNT(*) AS n FROM mdr_flat
        WHERE {WHERE} AND manufacturer IS NOT NULL AND trim(manufacturer) <> ''
        GROUP BY 1 ORDER BY n DESC
    """, PARAMS_T)
    if df.empty:
        st.info("Manufacturer is carried on the device record, which is absent "
                f"before {CODE_ELIGIBLE_FROM_YEAR}.")
    else:
        total = float(df["n"].sum())
        df["share_pct"] = (df["n"] / total * 100).round(3)
        hhi = float(((df["n"] / total * 100) ** 2).sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("Distinct name strings", fmt(len(df)))
        c2.metric("Top-1 share", f"{df['share_pct'].iloc[0]:.1f}%")
        c3.metric("HHI", f"{hhi:,.0f}",
                  help="Herfindahl-Hirschman Index over report shares. >2,500 is "
                       "conventionally 'highly concentrated'.")
        top = df.head(20).sort_values("n")
        fig = px.bar(top, y="manufacturer", x="n", orientation="h",
                     labels={"n": "Reports", "manufacturer": ""},
                     title=f"Top 20 of {fmt(len(df))} manufacturer name strings")
        fig.update_traces(marker_color="#1D6A96")
        st.plotly_chart(style_fig(fig, 540, margin=dict(l=250, r=20, t=44, b=44)),
                        use_container_width=True)
        st.warning(
            "**These are report counts, not market shares.** Manufacturer names in "
            "MAUDE are free text and are not normalised, so one company may appear "
            "under several spellings — which understates concentration. HHI here "
            "describes reporting share, not market share, and a high value may simply "
            "reflect one manufacturer's reporting practices.", icon="⚠️")
        download_row(df, "manufacturers")

elif analysis == "Reporting lag":
    st.subheader("Reporting lag (event → FDA receipt)")
    df = q(DB_PATH, f"""
        SELECT lag_days FROM mdr_flat WHERE {WHERE}
          AND lag_days IS NOT NULL AND lag_days BETWEEN 0 AND 3650 LIMIT 400000
    """, PARAMS_T)
    neg = nz(q(DB_PATH,
        f"SELECT COUNT(*) AS n FROM mdr_flat WHERE {WHERE} AND lag_days < 0",
        PARAMS_T).iloc[0]["n"])
    if df.empty:
        st.info("These reports lack either an event date or a received date.")
    else:
        s = df["lag_days"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Median", f"{s.median():.0f} d")
        c2.metric("IQR", f"{s.quantile(.25):.0f}–{s.quantile(.75):.0f} d")
        c3.metric("P90", f"{s.quantile(.9):.0f} d")
        c4.metric("Reports with lag", fmt(len(df)))
        fig = px.histogram(df, x="lag_days", nbins=60,
                           labels={"lag_days": "Days from event to FDA receipt"},
                           title="Lag distribution (0–3,650 days)")
        fig.update_traces(marker_color="#1D6A96")
        st.plotly_chart(style_fig(fig), use_container_width=True)
        if neg:
            st.warning(f"**{fmt(neg)} reports have a negative lag** — the recorded "
                       "event date falls after the received date. These are data-entry "
                       "artefacts, excluded from the chart and quantiles above.", icon="⚠️")
        download_row(df, "reporting_lag")

elif analysis == "Text mining":
    st.subheader("Narrative text mining")
    scan_cap = st.number_input("Narratives to analyse", 1_000, 500_000, 50_000, 5_000,
                               help="Tokenisation happens in Python; a large cap on a "
                                    "wide cohort is slow.")
    min_count = st.slider("Minimum phrase count", 1, 100, 10)
    extra_stop = st.text_input("Additional stopwords (comma-separated)", "")

    df = q(DB_PATH, f"""
        SELECT narrative_desc FROM mdr_flat WHERE {WHERE}
          AND narrative_desc IS NOT NULL LIMIT {int(scan_cap)}
    """, PARAMS_T)
    if df.empty:
        st.info("No report in this cohort carries description text.")
    else:
        base_stop = {
            "the", "and", "was", "were", "for", "that", "this", "with", "not", "has",
            "have", "had", "been", "are", "from", "which", "there", "their", "them",
            "patient", "device", "report", "reported", "event", "manufacturer",
            "information", "unknown", "date", "received", "stated", "indicated",
            "reportedly", "additional", "will", "also", "one", "two", "product",
            "returned", "evaluation", "complaint", "customer", "user", "facility",
            "further", "follow", "review", "time", "approximately", "due", "found",
            "noted", "none", "unk",
        } | {w.strip().lower() for w in extra_stop.split(",") if w.strip()}
        tok = re.compile(r"\b[a-z]{3,}\b")
        uni: Counter = Counter()
        bi: Counter = Counter()
        tri: Counter = Counter()
        for text in df["narrative_desc"].astype(str):
            toks = [w for w in tok.findall(text.lower()) if w not in base_stop]
            uni.update(toks)
            bi.update(" ".join(g) for g in zip(toks, toks[1:]))
            tri.update(" ".join(g) for g in zip(toks, toks[1:], toks[2:]))

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Single words**")
            w = pd.DataFrame(uni.most_common(200), columns=["Term", "Count"])
            w = w[w["Count"] >= min_count]
            st.dataframe(w.head(60), width="stretch", height=420, hide_index=True)
            download_row(w, "narrative_terms")
        with c2:
            st.markdown("**Phrases (2–3 words)**")
            ph = pd.DataFrame(
                sorted(list(bi.items()) + list(tri.items()),
                       key=lambda kv: -kv[1])[:400], columns=["Phrase", "Count"])
            ph = ph[ph["Count"] >= min_count]
            st.dataframe(ph.head(60), width="stretch", height=420, hide_index=True)
            download_row(ph, "narrative_phrases")

        try:
            from wordcloud import WordCloud
            if uni:
                wc = WordCloud(width=1200, height=460, background_color="white",
                               collocations=False).generate_from_frequencies(
                    dict(uni.most_common(300)))
                st.image(wc.to_array(), width="stretch")
        except ImportError:
            st.caption("Install `wordcloud` to render a term cloud here.")

        if len(df) >= scan_cap:
            st.warning(f"**Sampled.** Frequencies come from the first {fmt(len(df))} "
                       "narratives, not the whole cohort. Narrow the cohort or raise "
                       "the cap for a complete count.", icon="⚠️")
        st.caption("Narratives in `mdr_flat` are capped at 4,000 characters. The "
                   "untruncated multi-part text is in the `foi` table — see "
                   "**Narrative text**.")

elif analysis == "Narrative text":
    st.subheader("Narrative text")
    if HAS_FOI:
        wanted = st.multiselect(
            "Text type", ["D", "N", "M"], default=["D", "N", "M"],
            format_func=lambda c: {"D": "D — event description",
                                   "N": "N — manufacturer narrative",
                                   "M": "M — additional manufacturer narrative"}[c])
        type_filter = (f"AND f.TEXT_TYPE_CODE IN ({','.join(repr(w) for w in wanted)})"
                       if wanted else "")
        df = q(DB_PATH, f"""
            WITH keys AS (SELECT MDR_REPORT_KEY FROM mdr_flat WHERE {WHERE})
            SELECT f.MDR_REPORT_KEY, f.TEXT_TYPE_CODE, f.FOI_TEXT
            FROM foi f JOIN keys USING (MDR_REPORT_KEY)
            WHERE f.FOI_TEXT IS NOT NULL {type_filter} LIMIT {int(row_cap)}
        """, PARAMS_T)
        st.caption(f"Showing {fmt(len(df))} text segments — full untruncated text "
                   "from the `foi` table.")
    else:
        df = q(DB_PATH, f"""
            SELECT MDR_REPORT_KEY, DATE_PREF, EVENT_TYPE, narrative_desc, narrative_mfg
            FROM mdr_flat WHERE {WHERE} AND has_narrative
            ORDER BY DATE_PREF DESC NULLS LAST LIMIT {int(row_cap)}
        """, PARAMS_T)
        st.caption("The `foi` table is absent, so this shows the 4,000-character "
                   "narratives stored on `mdr_flat`.")
    if df.empty:
        st.info("No narrative text for this cohort.")
    else:
        show_table(df, 600)
        download_row(df, "narratives")

elif analysis == "Data quality":
    st.subheader("Data quality")
    st.caption("Check this before quoting any headline figure — missingness in MAUDE "
               "is structural, not random.")
    fields = [
        ("report_number", "Report number", "REPORT_NUMBER IS NOT NULL AND trim(REPORT_NUMBER) <> ''"),
        ("event_type", "Event type", "EVENT_TYPE IS NOT NULL AND trim(EVENT_TYPE) <> ''"),
        ("date_received", "Date received", "DATE_RECEIVED_D IS NOT NULL"),
        ("date_of_event", "Date of event", "DATE_OF_EVENT_D IS NOT NULL"),
        ("manufacturer", "Manufacturer", "manufacturer IS NOT NULL AND trim(manufacturer) <> ''"),
        ("product_code", "Product code", "product_code IS NOT NULL AND trim(product_code) <> ''"),
        ("reporter", "Reporter occupation", "REPORTER_OCCUPATION_CODE IS NOT NULL AND trim(REPORTER_OCCUPATION_CODE) <> ''"),
        ("source_type", "Source type", "SOURCE_TYPE IS NOT NULL AND trim(SOURCE_TYPE) <> ''"),
        ("narrative", "Narrative present", "has_narrative"),
        ("patient_age", "Patient age", "age_years_avg IS NOT NULL"),
        ("device_age", "Device age", "device_age_days IS NOT NULL"),
    ]
    if CLINICAL_READY:
        fields.append(("outcome", "Outcome coded",
                       "outcome_codes_raw IS NOT NULL AND trim(outcome_codes_raw) <> ''"))
    flags = [
        ("supplements", "Supplement submissions", "is_supplement"),
        ("rwd", "RWD-sourced", "IS_RWD_SOURCED"),
        ("forwarded", "Forwarded 803.22(b)(2)", "IS_FORWARDED_803_22_B2"),
        ("redaction_b4", "Redaction (b)(4)", "HAS_REDACTION_B4"),
        ("redaction_b6", "Redaction (b)(6)", "HAS_REDACTION_B6"),
    ]
    allf = fields + flags
    sel = ", ".join(f"AVG(CASE WHEN {e} THEN 1.0 ELSE 0.0 END) AS {k}" for k, _, e in allf)
    r = q(DB_PATH, f"SELECT {sel} FROM mdr_flat WHERE {WHERE}", PARAMS_T).iloc[0]

    for group_name, group, colour in (("Field completeness", fields, "#1D6A96"),
                                      ("Flag rates", flags, "#D9740B")):
        data = pd.DataFrame([{"Field": label, "Rate (%)": round(nz(r[k], float) * 100, 2)}
                             for k, label, _ in group]).sort_values("Rate (%)")
        fig = px.bar(data, x="Rate (%)", y="Field", orientation="h", range_x=[0, 100],
                     title=f"{group_name} (n = {fmt(N_TOTAL)})", labels={"Field": ""})
        fig.update_traces(marker_color=colour)
        st.plotly_chart(style_fig(fig, 60 + 30 * len(group),
                                  margin=dict(l=200, r=20, t=46, b=44)), use_container_width=True)

    pc_rate = nz(r["product_code"], float)
    if pc_rate < 0.95:
        st.warning(
            f"**Product code is only {pc_rate:.1%} complete in this cohort — and this is "
            "not random missingness.** The FDA device files in this build begin in "
            f"{CODE_ELIGIBLE_FROM_YEAR}, so every earlier report lacks device attributes "
            "entirely. Restrict the year range for any device-level analysis.", icon="⚠️")
    download_row(pd.DataFrame([{"Field": label, "Rate (%)": round(nz(r[k], float) * 100, 2)}
                               for k, label, _ in allf]), "data_quality")

elif analysis == "Methods & STROBE":
    st.subheader("Methods & reproducibility")
    n_unique = nz(q(DB_PATH,
        f"SELECT COUNT(DISTINCT REPORT_NUMBER) AS n FROM mdr_flat WHERE {WHERE}",
        PARAMS_T).iloc[0]["n"])

    crit = [f"Reports dated {year_range[0]} to {year_range[1]} inclusive, by preferred "
            "report date (DATE_RECEIVED where present, falling back to REPORT_DATE)."]
    if product_code:
        crit.append(f"FDA product classification code '{product_code.upper()}'.")
    if manufacturer:
        crit.append(f"Manufacturer name containing '{manufacturer}' (case-insensitive).")
    if device_terms:
        crit.append("Brand, generic or model name containing any of: "
                    + ", ".join(f"'{t}'" for t in device_terms) + " (case-insensitive).")
    if narrative:
        crit.append(f"Event description containing '{narrative}'.")
    if event_picks and len(event_picks) < 3:
        crit.append("Event types restricted to: "
                    + ", ".join(EVENT_LABELS[c] for c in event_picks) + ".")
    if exclude_forwarded:
        crit.append("Reports forwarded under 21 CFR 803.22(b)(2) were excluded.")
    if exclude_rwd:
        crit.append("Reports sourced from real-world data under the 21 CFR 803.19 "
                    "exemption were excluded.")
    if initial_only:
        crit.append("Supplemental submissions were excluded, retaining initial reports "
                    "only, to avoid double-counting events that received follow-up.")
    if implant_only:
        crit.append("Restricted to reports where IMPLANT_FLAG = 'Y'.")
    if serious_only:
        crit.append("Restricted to reports recording at least one serious patient "
                    "outcome (D, L, H, S, C or R per 21 CFR 803.3).")
    if mdr_key:
        crit.append(f"Single-report lookup: MDR_REPORT_KEY = {mdr_key}.")

    methods = f"""## Methods

We conducted a retrospective analysis of the United States Food and Drug
Administration Manufacturer and User Facility Device Experience (MAUDE)
database using the publicly distributed device-experience files (data current
to {FACTS['vintage']}). The MDR master, device, patient, foitext, foidevproblem
and patientproblemcode files were parsed, multi-line narrative records were
reassembled by a state-machine parser, and the data were loaded into a DuckDB
analytic database. A denormalised analytic table with one row per medical
device report (MDR_REPORT_KEY) was constructed; patient-level fields, including
SEQUENCE_NUMBER_OUTCOME, were aggregated to the report level, and device fields
were attached from the first device record per report ordered by
DEVICE_EVENT_KEY.

**Inclusion criteria.** {' '.join(crit)}

**Outcomes.** Patient outcomes were taken from SEQUENCE_NUMBER_OUTCOME and
dichotomised into the seven categories defined at 21 CFR 803.3: death (D),
life-threatening (L), hospitalization (H), disability (S), congenital anomaly
(C), required intervention (R) and other (O). A composite "any serious outcome"
was defined as any of D, L, H, S, C or R.

**Statistical analysis.** Proportions are reported with Wilson score 95%
confidence intervals. Comparisons between independent groups used Pearson's
chi-square test of independence; the minimum expected cell count was inspected
and results resting on expected counts below 5 were reported as descriptive.
Disproportionality analyses report the proportional reporting ratio and
reporting odds ratio with log-normal 95% confidence intervals and a 0.5
continuity correction, together with a Yates-corrected chi-square computed on
observed counts, and Fisher's exact test where any cell contained fewer than
10 observations. Two signal criteria were applied in parallel: the frequentist
EMA 2008 rule (PRR >= 2, chi-square >= 4, at least 3 reports in the cohort),
and the Bayesian Information Component of the WHO Uppsala Monitoring Centre,
where a signal requires the lower bound of the 95% credibility interval to
exceed zero (IC025 > 0), computed with the shrinkage form and closed-form
credibility bounds of Noren et al. (Stat Med 2006;25:3740-57). The Information
Component is reported alongside PRR because it is stable for rare codes, where
ratio measures are volatile and the EMA rule over-signals. Because several
hundred problem codes are screened simultaneously, p-values were adjusted for
multiple comparisons using the Benjamini-Hochberg false discovery rate
procedure, and adjusted q-values are reported. The disproportionality
comparator was restricted to the {fmt(FACTS['eligible'])} reports eligible to
carry a problem code, because the FDA problem-code files begin in
{CODE_ELIGIBLE_FROM_YEAR}. Temporal trends were assessed with the
Cochran-Armitage test for trend in proportions and the tie-corrected
Mann-Kendall non-parametric trend test on annual counts. Differences between
independent proportions are reported with Newcombe hybrid-score intervals
(Stat Med 1998;17:873-90).

**Cohort size.** {fmt(N_TOTAL)} reports met the inclusion criteria,
corresponding to {fmt(n_unique)} unique events by REPORT_NUMBER.

**Limitations.** MAUDE is a passive surveillance system without a defined
denominator of exposed devices; reported frequencies are proportions of reports
and cannot be interpreted as incidence. Reporting is subject to under-reporting,
stimulated reporting following publicity, differential reporting between
manufacturers and user facilities, and residual duplication despite supplement
exclusion. Outcome coding is voluntary and incomplete. Device-level fields are
unavailable for reports predating {CODE_ELIGIBLE_FROM_YEAR} in this build.
Disproportionality signals are hypothesis-generating and do not establish
causation.

**Software.** Analyses were performed with {APP_NAME} {VERSION} (DuckDB for
storage and query; statistics cross-validated against SciPy). Source code:
{REPO_URL}."""

    st.markdown(methods)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button("Download methods (.md)", methods.encode(),
                       f"maudedash_methods_{ts}.md", "text/markdown")

    st.markdown("#### Exact filter, for reproducibility")
    literal = WHERE
    for p_ in PARAMS:
        literal = literal.replace("?", repr(p_) if isinstance(p_, str) else str(p_), 1)
    st.code(f"-- Runnable against your maude_final.duckdb\n"
            f"SELECT COUNT(*) FROM mdr_flat\nWHERE {literal};", language="sql")
    st.caption("Parameter values are inlined above so the statement runs as-is. "
               "MAUDE is revised retroactively — record the data vintage "
               f"({FACTS['vintage']}) alongside any figure you publish.")

elif analysis == "Export":
    st.subheader("Cohort export")
    st.caption("The full analytic record per report. Generated on demand — nothing is "
               "serialised until you press a button.")
    cap = int(min(row_cap, 200_000))
    st.write(f"Cohort holds **{fmt(N_TOTAL)}** reports. Export is capped at "
             f"**{fmt(cap)}** rows (raise the sidebar row cap to increase).")
    if st.button("Build export", type="primary"):
        with st.spinner("Building export…"):
            df = q(DB_PATH, f"""
                SELECT * FROM mdr_flat WHERE {WHERE}
                ORDER BY DATE_PREF DESC NULLS LAST, MDR_REPORT_KEY LIMIT {cap}
            """, PARAMS_T)
        st.success(f"{fmt(len(df))} rows ready.")
        st.dataframe(pretty_columns(df.head(200)), width="stretch", height=380,
                     hide_index=True)
        download_row(df, "cohort")
        if N_TOTAL > cap:
            st.warning(f"Truncated: {fmt(N_TOTAL)} reports match but only {fmt(cap)} "
                       "were exported, ordered by date descending.", icon="⚠️")

st.markdown("---")
st.caption(
    f"**{APP_NAME}** {VERSION} · [maudedash.com]({SITE_URL}) · "
    f"[Paper]({PAPER_URL}) · [Source]({REPO_URL}) · FDA data to {FACTS['vintage']} · "
    "Independent research tool, not affiliated with or endorsed by the U.S. FDA."
)
