"""
vendor_assets.py — pull the browser runtime local, for archival stability
=========================================================================

By default the web tier loads DuckDB-WASM and Plotly from jsDelivr. That is
fine for day-to-day use, but a tool cited in a paper should not stop working
because a CDN changed. This script downloads the exact pinned versions into
web/vendor/ so the deployment has no third-party runtime dependency.

    python packaging/vendor_assets.py

Then set `wasmSource: 'local'` in web/assets/db.js and re-upload. Adds ~40 MB.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import os
import sys
import urllib.request

DUCKDB_VERSION = "1.29.0"
PLOTLY_VERSION = "3.0.1"

DUCKDB_BASE = f"https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@{DUCKDB_VERSION}/dist"
PLOTLY_URL = (f"https://cdn.jsdelivr.net/npm/plotly.js-dist-min@{PLOTLY_VERSION}"
              f"/plotly.min.js")

# The MVP bundle is the fallback for browsers without WebAssembly exception
# handling; duckdb-wasm picks between them at runtime, so both are needed.
DUCKDB_FILES = [
    "duckdb-mvp.wasm",
    "duckdb-browser-mvp.worker.js",
    "duckdb-eh.wasm",
    "duckdb-browser-eh.worker.js",
]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def fetch(url: str, dest: str, force: bool = False) -> bool:
    if os.path.exists(dest) and not force:
        print(f"  = {os.path.basename(dest):<34} already present "
              f"({human(os.path.getsize(dest))})")
        return True
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MaudeDash/vendor"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(tmp, dest)
        digest = hashlib.sha256(open(dest, "rb").read()).hexdigest()[:16]
        print(f"  + {os.path.basename(dest):<34} {human(os.path.getsize(dest)):>10}"
              f"  sha256:{digest}")
        return True
    except Exception as exc:
        print(f"  ! {os.path.basename(dest):<34} FAILED: {exc}", file=sys.stderr)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


ESM_SPEC = re.compile(r'/npm/((?:@[^/@"]+/)?[^/@"]+)@([^/"]+)/\+esm')


def safe_name(pkg: str, ver: str) -> str:
    return f"{pkg.replace('/', '__').lstrip('@')}@{ver}.mjs"


def vendor_esm_graph(root_pkg: str, root_ver: str, dest_dir: str,
                     force: bool = False) -> tuple[str, int]:
    """Fetch a jsDelivr +esm module and everything it imports, rewriting the
    cross-origin specifiers to local files.

    dist/duckdb-browser.mjs cannot be used directly: it is the unbundled build
    and imports bare specifiers such as "apache-arrow", which a browser cannot
    resolve without a bundler or an import map. jsDelivr's /+esm endpoint serves
    a bundled variant instead, but that one references its dependencies by
    absolute jsDelivr URL — which reintroduces the third-party origin we are
    trying to remove.

    So: walk the graph, save each module locally, and rewrite every
    /npm/pkg@ver/+esm reference to the corresponding local filename. The graph
    is small and closed (duckdb -> apache-arrow -> flatbuffers, tslib).
    """
    os.makedirs(dest_dir, exist_ok=True)
    queue = [(root_pkg, root_ver)]
    seen: set[tuple[str, str]] = set()
    count = 0

    while queue:
        pkg, ver = queue.pop(0)
        if (pkg, ver) in seen:
            continue
        seen.add((pkg, ver))

        fname = safe_name(pkg, ver)
        path = os.path.join(dest_dir, fname)
        url = f"https://cdn.jsdelivr.net/npm/{pkg}@{ver}/+esm"

        if os.path.exists(path) and not force:
            body = open(path, "r", encoding="utf-8").read()
            print(f"  = {fname:<40} already present ({human(len(body))})")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "MaudeDash/vendor"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
            count += 1

        deps = set(ESM_SPEC.findall(body))
        for dpkg, dver in deps:
            queue.append((dpkg, dver))

        rewritten = ESM_SPEC.sub(
            lambda m: "./" + safe_name(m.group(1), m.group(2)), body)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rewritten)
        if deps:
            print(f"  + {fname:<40} {human(len(rewritten)):>10}  "
                  f"deps: {', '.join(sorted(p for p, _ in deps))}")
        else:
            print(f"  + {fname:<40} {human(len(rewritten)):>10}")

    return safe_name(root_pkg, root_ver), count


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Vendor the MaudeDash browser runtime")
    ap.add_argument("--out", default=os.path.join(here, "..", "web", "vendor"))
    ap.add_argument("--force", action="store_true", help="Re-download existing files")
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    duck_dir = os.path.join(out, "duckdb")
    plot_dir = os.path.join(out, "plotly")
    os.makedirs(duck_dir, exist_ok=True)
    os.makedirs(plot_dir, exist_ok=True)

    print(f"Vendoring into {out}\n")
    print(f"DuckDB-WASM {DUCKDB_VERSION} — WebAssembly modules and workers")
    ok = all(fetch(f"{DUCKDB_BASE}/{name}", os.path.join(duck_dir, name), args.force)
             for name in DUCKDB_FILES)

    print(f"\nDuckDB-WASM {DUCKDB_VERSION} — bundled ESM graph")
    esm_dir = os.path.join(out, "esm")
    try:
        entry, n_new = vendor_esm_graph("@duckdb/duckdb-wasm", DUCKDB_VERSION,
                                        esm_dir, args.force)
        print(f"  entry point: vendor/esm/{entry}  ({n_new} newly downloaded)")
    except Exception as exc:
        print(f"  ! ESM graph failed: {exc}", file=sys.stderr)
        ok = False

    print(f"\nPlotly {PLOTLY_VERSION}")
    ok = fetch(PLOTLY_URL, os.path.join(plot_dir, "plotly.min.js"), args.force) and ok

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(out) for f in fs)
    print(f"\nTotal vendored: {human(total)}")

    if ok:
        print(
            "\nDone. Two things left:\n"
            "  1. In web/assets/db.js set  wasmSource: 'local'\n"
            "  2. Upload web/vendor/ alongside the rest of the site\n"
            "\nindex.html already prefers the local Plotly and falls back to the CDN,\n"
            "so Plotly needs no code change."
        )
    else:
        print("\nOne or more downloads failed; the CDN default still works.",
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
