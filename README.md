<div align="center">

<img src="web/assets/logo.svg" width="72" alt="">

# MaudeDash

**Publication-grade analytics for the U.S. FDA MAUDE medical device adverse event database**

[maudedash.com](https://maudedash.com) ·
[Paper (PubMed 42232423)](https://pubmed.ncbi.nlm.nih.gov/42232423/) ·
[FDA MAUDE](https://www.fda.gov/medical-devices/mandatory-reporting-requirements-manufacturers-importers-and-device-user-facilities/manufacturer-and-user-facility-device-experience-database-maude)

20.7 million reports · 1991–2024 · FDA 21 CFR 803.3 harm classification ·
Wilson confidence intervals · PRR/ROR/IC signal screening with FDR control ·
searchable by device name · STROBE-ready methods

</div>

---

## Two ways to run it

| | **Web tier** | **Research Edition** |
|---|---|---|
| Where | [maudedash.com](https://maudedash.com) — any browser | Your own machine |
| Install | none | `pip install -r requirements.txt` |
| Data | 1.4 GB Parquet, fetched in slices as needed | The full ~73 GB DuckDB you build locally |
| Engine | DuckDB-WASM, in the browser | DuckDB + Streamlit |
| Narratives | 4,000 characters per report | Complete, untruncated, multi-part |
| Server needed | **None** — static files only | Local only |

Both tiers run the same 22 analyses and the same statistics, cross-validated
against SciPy to 1e-9 or better on every function.

---

## Quick start — Research Edition

```bash
pip install -r requirements.txt
```

```bash
python maude_build.py --raw-dir /path/to/fda/files --db maude_final.duckdb --skip-fts
```

```bash
streamlit run maudedash_app.py -- --db maude_final.duckdb
```

Source files come from the FDA's
[MDR data files](https://www.fda.gov/medical-devices/medical-device-reporting-mdr-how-report-medical-device-problems/mdr-data-files)
page. The build is resumable — re-run the same command after a crash and it
picks up where it stopped.

Verify the install with `python test_maude.py` (25 tests).

## Quick start — publish the web tier

```bash
python packaging/maude_export_web.py --db maude_final.duckdb --out web/data
python packaging/build_search_index.py --db maude_final.duckdb --out web/data
```

```bash
python packaging/serve_local.py
```

Then upload `web/` to any static host. See
[deploy/DREAMHOST.md](deploy/DREAMHOST.md) for the full procedure, including
the `.htaccess` that makes HTTP range requests work.

---

## How 73 GB becomes 1.4 GB

The database is dominated by things no analysis reads:

| Dropped | Size |
|---|---|
| DuckDB full-text index (`terms`, 1.54 billion rows) | the bulk of it |
| Raw staging tables (`mdr`, `device`, `patient`), already denormalised into `mdr_flat` | large |
| Five lowercased duplicate columns, including a **second full copy of every narrative** | ~1.1 GB |

What ships is `mdr_flat` itself — one row per report, all 4,000-character
narratives intact — plus the problem-code bridges, the code dictionaries and the
rollups. That is full analytic parity at **1/53rd the size**.

Each year is a separate Parquet file sorted by `product_code`, so the browser
opens only the years in range and reads only the columns a panel asks for. A
typical session transfers **~14 MB**, not 1.4 GB.

---

## Analyses

**Overview** — reports, yearly volume, event types
**Clinical** — FDA 7-harm outcomes with Wilson CIs, demographics, device age at failure, death deep-dive
**Signals** — device and patient problem codes, disproportionality (PRR/ROR, Information Component with IC025, Yates χ², Fisher, EMA-2008 and WHO-UMC criteria, FDR q-values)
**Inference** — subgroup forest plots, Cochran-Armitage and Mann-Kendall trend tests, cohort comparison, sensitivity scenarios
**Context** — reporter and source, geography, manufacturer concentration, reporting lag
**Narrative** — term and phrase mining, full narrative browsing
**Report** — data quality, auto-generated STROBE methods, cohort export

---

## Statistics

Implemented in `maude_stats.py` (Python) and `web/assets/stats.js` (browser),
cross-validated against each other and against SciPy:

Wilson score intervals · Cochran-Armitage trend · Mann-Kendall with tie
correction · Yates-corrected χ² · Pearson χ² with Cochran's-rule reporting ·
Fisher's exact · PRR with Sahai-Khurshid log SE · ROR with Wald log SE ·
**Information Component (BCPNN) with IC025 credibility bounds** ·
**Benjamini-Hochberg FDR** · **Newcombe hybrid-score difference intervals** ·
EMA-2008 signal rule

Signal detection applies two criteria in parallel — the frequentist EMA-2008
rule and the WHO-UMC Bayesian Information Component — because PRR is volatile
for rare codes, exactly where MAUDE screening is most fragile. Codes flagged by
both are the defensible set. Because a screen tests hundreds of codes at once,
q-values are reported alongside raw p-values.

---

## Corrections in v5

These change numbers relative to the v4 code used for the published analysis.
The v4 files are retained unmodified for provenance.

1. **Disproportionality comparator.** v4 divided by all 20,746,963 reports. The
   FDA problem-code files begin in 2015, so 4,195,649 reports (20.2%) cannot
   carry a code. Including them **inflated every PRR by a factor of ~1.25** and
   manufactured signals at the EMA threshold of PRR ≥ 2. The comparator is now
   the 16,551,314 code-eligible reports, and the figure is stated on screen.
2. **Yates χ²** is computed on observed counts with the correction clamped at
   zero. v4 fed the 0.5-corrected cells in and did not clamp, overstating χ² by
   **39–49% on sparse tables** and scoring a perfectly null table (5,5,5,5) as
   0.18 instead of 0. Verified against `scipy.stats.chi2_contingency`.
3. **Mann-Kendall** applies the standard tie correction, which v4 documented but
   did not implement.
4. **PRR/ROR intervals** use the exact 95% z rather than a hardcoded 1.96.
5. **Death count** is a true `COUNT(*)`; v4 displayed `len(df)` after a
   `LIMIT 5000`, so any cohort with more than 5,000 deaths read exactly "5,000".
6. **Ingest compatibility.** v4 passed `strict_mode` to `read_csv`, which only
   exists in DuckDB ≥ 1.2. On DuckDB 1.1.x every load failed — and the loader
   logged the error and returned 0 instead of raising, so the build **exited
   successfully with no data**. The option is now probed for.
7. **Concurrency.** Queries run on per-call cursors. The shared cached
   connection returned empty results for 776 of 800 concurrent queries in
   testing.
8. **WAL preservation.** The builder no longer deletes `*.duckdb.wal`, which
   discarded committed-but-uncheckpointed work on every run.

---

## Known limitations

1. **MAUDE is passive surveillance.** There is no denominator of exposed
   devices. Every rate here is a proportion of *reports*, never an incidence.
2. **Device data begins in 2015** in this build. Reports from 1991–2014 exist in
   the MDR master but carry no product code, manufacturer, brand, narrative or
   problem code. Any cohort defined by those fields silently contains no
   pre-2015 reports.
3. **Outcome coding is voluntary.** A report without an outcome code is not
   necessarily benign.
4. **Manufacturer names are free text** and are not normalised, so concentration
   measures understate reality.
5. **Disproportionality signals are hypothesis-generating**, never causal.
   Benjamini-Hochberg q-values are reported across the codes screened, but no
   correction exists for the deeper problem: MAUDE has no exposure denominator,
   so a ratio cannot be turned into a risk.
6. **The most recent year is usually partial**, since the FDA files are captured
   mid-cycle.

---

## Cite

> Porwal M. MaudeDash: an open analytic platform for the FDA MAUDE medical
> device adverse event database. *Surg Neurol Int.* 2026.
> doi:10.25259/SNI_201_2026. Available from: https://maudedash.com

Record the data vintage shown in the app header — MAUDE is revised
retroactively, so a query re-run later may return different counts.

---

## Repository layout

```
research-edition/     Streamlit app, build pipeline, statistics, tests
packaging/            Web-tier export, asset vendoring, local preview server
web/                  The static site (index.html, assets, .htaccess, data/)
deploy/               DreamHost guide, Dockerfile, fly.toml
```

## License

MIT. MaudeDash is an independent research tool and is not affiliated with,
endorsed by, or reviewed by the U.S. Food and Drug Administration.
