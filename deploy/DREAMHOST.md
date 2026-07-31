# Deploying MaudeDash to DreamHost shared hosting

This is the full procedure for putting MaudeDash.com live on your existing
shared plan. It takes one upload and no server-side configuration beyond the
`.htaccess` that ships with the site.

---

## Why the tool is built this way

You asked whether Streamlit could run on DreamHost shared. It cannot, and it is
worth stating plainly so the decision is documented:

- Streamlit is a **Tornado server with a WebSocket session per visitor**. It is
  not a WSGI application, so DreamHost's Passenger cannot host it.
- Shared plans **terminate long-running user processes**. Even started over SSH,
  a Streamlit daemon is killed by DreamHost's process watcher.
- A DuckDB query against the full corpus wants gigabytes of RAM. Shared hosting
  caps process memory well below that.

So MaudeDash ships as two tiers:

| Tier | Runs where | What it is |
|---|---|---|
| **Web tier** | MaudeDash.com, DreamHost shared | Static files. DuckDB-WASM executes SQL **in the visitor's browser** over Parquet fetched with HTTP range requests. Zero server processes, zero server RAM, zero CPU. |
| **Research Edition** | The researcher's own machine | The full Streamlit app plus the build pipeline, for the complete ~73 GB corpus with untruncated narratives. Offered as a download. |

The web tier is what makes the site a *live tool* rather than a landing page,
and it is what a reviewer or a citing researcher will actually use.

---

## What you upload

```
maudedash.com/                 ← your domain's web root
├── .htaccess                  ← ships with the site; do not skip it
├── index.html
├── 404.html  methods.html  terms.html  privacy.html  accessibility.html
├── app.html                   ← the tool itself
├── robots.txt  sitemap.xml
├── .well-known/security.txt
├── assets/
│   ├── app.js  db.js  stats.js  site.js  theme.js  boot.js
│   ├── styles.css  site.css  logo.svg  favicon.svg
├── vendor/                    ← REQUIRED, ~79 MB (see "Self-hosting the engine")
└── data/                      ← 1.4 GB, produced by maude_export_web.py
    ├── manifest.json  summary.json
    ├── product_index.json  manufacturer_index.json
    ├── mdr_pre2014.parquet … mdr_2024.parquet   (12 files)
    ├── devprob.parquet  patprob.parquet
    ├── dict_device.parquet  dict_patient.parquet
    └── agg_*.parquet
```

**Total: about 1.4 GB.** DreamHost shared plans advertise unlimited storage; 1.4 GB
of static research data sits comfortably inside normal use. The important number
is not storage but bandwidth, and that is covered below.

---

## Step 1 — build the data (on your machine, once per FDA release)

```bash
python packaging/maude_export_web.py --db maude_final.duckdb --out web/data
python packaging/build_search_index.py --db maude_final.duckdb --out web/data
```

The first command takes roughly 18 minutes for the full corpus. The second takes
a few seconds and produces the type-ahead indexes that let visitors search
product codes by device name ("pedicle screw") rather than having to know that
the code is KWP. Together they add about 1.2 MB.

The export prints a per-file summary and writes `manifest.json` with SHA-256
checksums so you can verify the upload.

Options worth knowing:

```bash
# structured data only — no narrative text, ~700 MB instead of ~1.4 GB
python packaging/maude_export_web.py --db maude_final.duckdb --out web/data --no-narratives

# also ship the untruncated multi-part narratives from the foi table (+~2.2 GB)
python packaging/maude_export_web.py --db maude_final.duckdb --out web/data --include-foi
```

`--include-foi` is usually not worth it: only 0.33% of narrative segments exceed
the 4,000-character cap already present in `mdr_flat`, so you would more than
double the payload for a fraction of a percent more text.

---

## Step 2 — test locally before uploading

```bash
python packaging/serve_local.py
```

Then open http://127.0.0.1:8777. This server implements HTTP range requests and
the same MIME types Apache will use, so if it works here it will work on
DreamHost. Python's built-in `http.server` does **not** support ranges — do not
use it to test.

---

## Step 3 — upload

Use SFTP (FileZilla, Cyberduck, or `rsync` over SSH). Upload the entire `web/`
directory **contents** into your domain's web root.

```bash
rsync -avz --progress web/ USERNAME@SERVER.dreamhost.com:~/maudedash.com/
```

`rsync` is strongly preferred over a GUI client here: 1.4 GB across 20-odd files
resumes cleanly if the connection drops, and re-running it after a data refresh
transfers only what changed.

Confirm `.htaccess` made it — many SFTP clients hide dotfiles by default:

```bash
ssh USERNAME@SERVER.dreamhost.com "ls -la ~/maudedash.com/.htaccess"
```

---

## Step 4 — enable HTTPS

In the DreamHost panel: **Websites → Manage → Secure Certificate → add a free
Let's Encrypt certificate** for maudedash.com. The shipped `.htaccess` already
redirects HTTP to HTTPS once the certificate exists.

---

## Step 5 — verify

```bash
# the page loads
curl -sI https://maudedash.com/ | head -3

# Parquet is served with the right type and, crucially, honours ranges
curl -sI https://maudedash.com/data/mdr_2023.parquet | grep -iE "content-type|accept-ranges"

# a range request returns 206, not 200
curl -s -o /dev/null -w "%{http_code}\n" -r 0-99 https://maudedash.com/data/mdr_2023.parquet
```

You want `206`. If you get `200`, ranges are not working and the browser will
try to download whole files — recheck `.htaccess`.

Then confirm the engine is being compressed, which is worth 27 MB on every
first visit:

```bash
curl -sI -H "Accept-Encoding: gzip" https://maudedash.com/vendor/duckdb/duckdb-eh.wasm | grep -iE "content-encoding|content-length"
```

You want `content-encoding: gzip` and a length near 6.8 MB. If the header is
absent and the length is ~34 MB, `mod_deflate` is not applying to
`application/wasm` — check that the `AddType application/wasm .wasm` line in
`.htaccess` uploaded, since the compression rule depends on it.

Then open the site and run a cohort (product code `KWP`, 2018–2024). The
browser console should be clean.

---

## Capacity: will this survive traffic?

Short answer: there is nothing on the server that can crash. Apache sends
static bytes and does nothing else — no PHP, no Python, no database, no
long-running process. A request cannot consume CPU, cannot exhaust a
connection pool, and cannot deadlock, because no code executes server-side.
All computation happens in the visitor's browser, so **load scales with their
hardware, not yours**.

What that leaves is bandwidth and request count.

### Measured, on the real corpus

| | First visit | Repeat visit |
|---|---|---|
| Query engine (WebAssembly, gzipped) | 6.8 MB | 0 — cached, immutable |
| Plotly + application code (gzipped) | ~1.5 MB | 0 — cached |
| Indexes and manifest (gzipped) | ~0.3 MB | 0 — cached |
| Parquet data actually read | ~9 MB | ~9 MB |
| **Total** | **~18 MB** | **~9 MB** |

The Parquet figure is for a full session: loading a cohort and opening the
heaviest analysis panel. It is not 1.4 GB because Parquet is columnar, each
file holds one report year, and the problem-code index is sorted by product
code — a query for KWP reads 0.31 MB of a 58 MB file.

At ~18 MB per new visitor, **1,000 visits a month is roughly 15 GB**, and
returning visitors cost half that. That is unremarkable traffic for a shared
plan. A front-page news link driving 50,000 visits in a day would be about
900 GB, which is the point at which you would want a CDN in front — see below.

### The one number worth watching

A session issues roughly **165 HTTP requests**, most of them small range
requests against Parquet. That is high compared with an ordinary web page.
It is fine here because the server negotiates HTTP/2 (confirmed: the live site
advertises `Upgrade: h2`), which multiplexes them over a single connection
rather than opening 165 of them. If DreamHost ever disabled HTTP/2 for the
domain, per-connection limits would become the binding constraint before
bandwidth did.

### Already in place

- `Cache-Control: immutable` on the engine and data, so repeat visitors
  re-download nothing.
- Hotlink protection on `.parquet` and `.wasm`, so another site cannot embed
  your data and spend your bandwidth.
- `robots.txt` disallows `/data/`, and `.htaccess` blocks mirroring tools and
  SEO crawlers from the data files — a crawler recursively pulling 1.4 GB is
  the most realistic way to run up a bill.
- No directory indexes, so the payload cannot be enumerated.

### If it ever does get hammered

Put Cloudflare's free tier in front of the domain. Because every data file is
immutable and cacheable, Cloudflare will absorb essentially all of it after the
first request, and your origin bandwidth drops to near zero. This requires no
change to the site — only a DNS move. It is the correct escalation, and it is
worth knowing about in advance rather than discovering under load.

## Self-hosting the engine (required — already the default)

The DuckDB-WASM engine and Plotly are served from your own domain, not a CDN.
This is the shipped default (`wasmSource: 'local'` in `web/assets/db.js`) for
three reasons: no third-party runtime dependency for a tool cited in a paper,
no third-party request from a visitor's browser — which is what the privacy
statement promises — and no cross-origin script in the SQL worker.

The files are produced by:

```bash
python packaging/vendor_assets.py
```

That writes about 79 MB into `web/vendor/`: the WebAssembly modules, the worker
scripts, Plotly, and DuckDB's bundled ESM dependency graph with every
cross-origin import rewritten to a local file. **`web/vendor/` must be
uploaded** — without it the tool will not start.

### A note on the Content-Security-Policy

The shipped policy sets `object-src 'none'`, `base-uri`, `frame-ancestors`,
`form-action`, `style-src`, `img-src` and `font-src`, but deliberately omits
`script-src` and `default-src`.

That is not an oversight. DuckDB-WASM instantiates its WebAssembly module inside
a Web Worker, and under any restrictive `script-src` — including
`'self' 'wasm-unsafe-eval' 'unsafe-eval' blob: data:` with the engine fully
self-hosted — it fails with `RuntimeError: table index is out of bounds`, thrown
from inside the module, with no CSP violation reported to the page because the
block happens in the worker. Bisecting the policy showed the tool only runs when
`script-src` carries **both** `'unsafe-inline'` and `'unsafe-eval'` — which
together provide essentially no XSS protection while looking strict to a
scanner.

Since the site is static, with no server-side code, no forms, no accounts, no
cookies and no user-generated content rendered into the page, there is no
session to steal and no input to inject. An honest policy is better than a
theatrical one. A strict variant is included commented-out in `.htaccess`; if
you verify it works in your target browsers, enable it — but test that a cohort
actually loads, not merely that the page renders.

---

## Refreshing when the FDA publishes new data

1. Download the new MAUDE files from the FDA.
2. Rebuild: `python maude_build.py --raw-dir . --db maude_final.duckdb --skip-fts`
3. Re-export: `python packaging/maude_export_web.py --db maude_final.duckdb --out web/data`
4. Rebuild the pickers: `python packaging/build_search_index.py --db maude_final.duckdb --out web/data`
5. `rsync` the `data/` folder up again.

`summary.json` and `manifest.json` carry the data vintage, and the site displays
it in the header and the About panel, so the published date updates itself.

---

## If something goes wrong

**"The data files could not be loaded"** on the boot screen — `data/` did not
upload, or ranges are not working. Run the `curl` checks in Step 5.

**Blank page, console shows a WASM MIME error** — `.htaccess` is missing or
DreamHost has `mod_mime` disabled for the directory. Confirm the file uploaded.

**Queries are very slow** — check that `.parquet` is not being gzipped
(`curl -sI ... | grep -i content-encoding` should return nothing). Compressing
Parquet defeats range requests.

**It works locally but not live** — almost always the dotfile. `.htaccess` is
the single most commonly missed upload.

---

## Alternatives, for the record

You asked whether somewhere else would be cheaper or better. For this
architecture, no — the static tier costs nothing beyond hosting you already pay
for, never sleeps, and has no process to crash. But if you later want the full
Streamlit app hosted rather than downloaded:

| Option | Cost | Trade-off |
|---|---|---|
| **Hugging Face Spaces** | Free (16 GB RAM, 50 GB disk) | Sleeps after inactivity; URL is HF-branded unless upgraded. A recognised venue for research tooling, which reads well in a petition. |
| **Fly.io** | ~$3–5/mo | Custom domain works; small persistent volume; brief cold starts. |
| **Render / Railway** | ~$7/mo | Simple deploys; free tiers sleep. |
| **Streamlit Community Cloud** | Free | 1 GB resource cap — too small for this corpus. Not viable. |

`deploy/Dockerfile` and `deploy/fly.toml` in this repo will run the Research
Edition against a slimmed database on any of the first three. The static tier at
maudedash.com stays up regardless, which is the point.
