"""
build_search_index.py — searchable pickers for product codes and manufacturers
==============================================================================

MaudeDash is unusable to a newcomer unless they already know that KWP means
"pedicle screw system". This builds two small JSON indexes so the sidebar can
offer type-ahead search by plain device name:

    product_index.json       ~3,700 FDA product codes, each with the device
                             names that actually appear under it in MAUDE,
                             report/death counts and the years it spans
    manufacturer_index.json  top manufacturers by report volume

Both are tiny (a few hundred KB) and load once at startup, so search is instant
and costs no Parquet reads.

    python packaging/build_search_index.py --db maude_final.duckdb --out ../web/data
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import duckdb

# Names that carry no information and should never be a code's label.
JUNK_NAMES = {
    "", "UNKNOWN", "UNK", "N/A", "NA", "NONE", "NOT APPLICABLE", "NOT AVAILABLE",
    "NO DATA", "NULL", "SEE NARRATIVE", "SEE ABOVE", "DEVICE", "UNKNOWN DEVICE",
    "*", "-", "--", "...", "REDACTED",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build MaudeDash search indexes")
    ap.add_argument("--db", default="maude_final.duckdb")
    ap.add_argument("--out", default=os.path.join("..", "web", "data"))
    ap.add_argument("--min-year", type=int, default=2015,
                    help="Only summarise reports from this year onward, since "
                         "device names do not exist before 2015 (default 2015)")
    ap.add_argument("--max-manufacturers", type=int, default=4000)
    ap.add_argument("--mem-limit", default="8GB")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 2
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    con = duckdb.connect(args.db, read_only=True)
    con.execute(f"SET memory_limit='{args.mem_limit}'")

    junk_sql = ", ".join(f"'{j}'" for j in sorted(JUNK_NAMES) if j)

    # ------------------------------------------------------------- products
    print("Building product-code index…")
    t0 = time.time()
    rows = con.execute(f"""
        WITH scoped AS (
            SELECT product_code, GENERIC_NAME, BRAND_NAME, EVENT_TYPE,
                   any_serious_outcome, report_year
            FROM mdr_flat
            WHERE product_code IS NOT NULL AND TRIM(product_code) <> ''
              AND report_year >= {int(args.min_year)}
        ),
        -- The device name most often recorded under each code. MAUDE generic
        -- names are free text, so the modal value is the honest label.
        names AS (
            SELECT product_code, UPPER(TRIM(GENERIC_NAME)) AS nm, COUNT(*) AS n,
                   ROW_NUMBER() OVER (PARTITION BY product_code ORDER BY COUNT(*) DESC) AS rk
            FROM scoped
            WHERE GENERIC_NAME IS NOT NULL
              AND UPPER(TRIM(GENERIC_NAME)) NOT IN ({junk_sql})
              AND LENGTH(TRIM(GENERIC_NAME)) > 2
            GROUP BY 1, 2
        ),
        brands AS (
            SELECT product_code, UPPER(TRIM(BRAND_NAME)) AS nm, COUNT(*) AS n,
                   ROW_NUMBER() OVER (PARTITION BY product_code ORDER BY COUNT(*) DESC) AS rk
            FROM scoped
            WHERE BRAND_NAME IS NOT NULL
              AND UPPER(TRIM(BRAND_NAME)) NOT IN ({junk_sql})
              AND LENGTH(TRIM(BRAND_NAME)) > 2
            GROUP BY 1, 2
        ),
        stats AS (
            SELECT product_code,
                   COUNT(*) AS n_reports,
                   SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS n_deaths,
                   SUM(CASE WHEN any_serious_outcome THEN 1 ELSE 0 END) AS n_serious,
                   MIN(report_year) AS y0, MAX(report_year) AS y1
            FROM scoped GROUP BY 1
        )
        SELECT s.product_code AS code, s.n_reports, s.n_deaths, s.n_serious,
               s.y0, s.y1,
               list(DISTINCT n.nm ORDER BY n.nm) FILTER (WHERE n.rk <= 3) AS generic_names,
               list(DISTINCT b.nm ORDER BY b.nm) FILTER (WHERE b.rk <= 2) AS brand_names
        FROM stats s
        LEFT JOIN names  n ON n.product_code = s.product_code AND n.rk <= 3
        LEFT JOIN brands b ON b.product_code = s.product_code AND b.rk <= 2
        GROUP BY 1,2,3,4,5,6
        ORDER BY s.n_reports DESC
    """).fetchall()

    products = []
    for code, n_rep, n_death, n_ser, y0, y1, generics, brands in rows:
        generics = [g for g in (generics or []) if g]
        brands = [b for b in (brands or []) if b]
        label = generics[0] if generics else (brands[0] if brands else code)
        products.append({
            "code": code,
            "label": label.title() if label != code else code,
            "names": generics[:3],
            "brands": brands[:2],
            "n": int(n_rep),
            "deaths": int(n_death or 0),
            "serious": int(n_ser or 0),
            "y0": int(y0) if y0 is not None else None,
            "y1": int(y1) if y1 is not None else None,
        })
    print(f"  {len(products):,} product codes in {time.time() - t0:.0f}s")

    # -------------------------------------------------------- manufacturers
    print("Building manufacturer index…")
    t0 = time.time()
    mrows = con.execute(f"""
        SELECT manufacturer AS name, COUNT(*) AS n,
               SUM(CASE WHEN EVENT_TYPE='D' THEN 1 ELSE 0 END) AS deaths,
               MIN(report_year) AS y0, MAX(report_year) AS y1
        FROM mdr_flat
        WHERE manufacturer IS NOT NULL AND TRIM(manufacturer) <> ''
          AND UPPER(TRIM(manufacturer)) NOT IN ({junk_sql})
          AND report_year >= {int(args.min_year)}
        GROUP BY 1 ORDER BY n DESC LIMIT {int(args.max_manufacturers)}
    """).fetchall()
    manufacturers = [{
        "name": name, "n": int(n), "deaths": int(d or 0),
        "y0": int(y0) if y0 is not None else None,
        "y1": int(y1) if y1 is not None else None,
    } for name, n, d, y0, y1 in mrows]
    print(f"  {len(manufacturers):,} manufacturers in {time.time() - t0:.0f}s")

    con.close()

    for payload, fname in ((products, "product_index.json"),
                           (manufacturers, "manufacturer_index.json")):
        path = os.path.join(out, fname)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        print(f"  wrote {fname}  {os.path.getsize(path) / 1024:,.0f} KB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
