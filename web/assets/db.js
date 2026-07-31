/*
 * db.js — DuckDB-WASM data layer for MaudeDash
 * ============================================
 *
 * Queries the FDA MAUDE corpus entirely inside the browser. There is no
 * application server: the host serves static Parquet files, DuckDB-WASM reads
 * them over HTTP range requests, and only the byte ranges a query actually
 * touches are transferred.
 *
 * Why this stays cheap on shared hosting
 * --------------------------------------
 * Parquet is columnar and each mdr_YYYY.parquet holds exactly one report year,
 * so `report_year` has min == max per file. Two kinds of pruning follow:
 *
 *   file-level    the year range in the sidebar selects the file list, so
 *                 out-of-range years are never opened at all
 *   column-level  a query over product_code transfers the product_code column
 *                 chunks and nothing else — the narrative columns, which are
 *                 ~60% of the payload, stay on the server unless a tab asks
 *                 for narrative text
 *
 * Files are additionally sorted by product_code, so row-group statistics let
 * DuckDB skip most row groups for the single most common filter in the tool.
 */

'use strict';

/* -------------------------------------------------------------- configuration */

export const CONFIG = {
  // Where the Parquet payload lives, relative to index.html.
  dataDir: 'data/',

  /*
   * DuckDB-WASM delivery.
   *
   *   'local' — serve the engine from web/vendor/duckdb/ (the default)
   *   'cdn'   — fetch it from jsDelivr instead
   *
   * 'local' is the default for three reasons, in order of importance:
   *
   *   1. It works under a strict Content-Security-Policy. Loading the engine
   *      cross-origin means the SQL worker imports a script from another
   *      origin, and the resulting failure surfaces as an opaque
   *      "RuntimeError: table index is out of bounds" from inside the WASM
   *      module, with no CSP violation reported to the page. Same-origin
   *      delivery removes that failure mode entirely.
   *   2. No third-party runtime dependency, which matters for a tool cited in
   *      a paper: the site cannot break because a CDN changed.
   *   3. No third-party request from the visitor's browser, which is what the
   *      privacy statement promises.
   *
   * Populate web/vendor/ with `python packaging/vendor_assets.py` (~79 MB).
   * If that directory is absent, switch this to 'cdn' — but note the CSP in
   * web/.htaccess must then allow https://cdn.jsdelivr.net.
   */
  wasmSource: 'local',
  wasmVersion: '1.29.0',

  // WebAssembly modules and the worker scripts (from the package's dist/).
  localWasmDir: 'vendor/duckdb/',

  // The JavaScript entry point. This is the BUNDLED build produced by
  // jsDelivr's /+esm endpoint, with its dependency graph vendored alongside
  // and every cross-origin specifier rewritten to a local file by
  // packaging/vendor_assets.py. The package's own dist/duckdb-browser.mjs
  // cannot be used here: it is unbundled and imports bare specifiers such as
  // "apache-arrow", which no browser can resolve without a bundler.
  localEsmEntry: 'vendor/esm/duckdb__duckdb-wasm@1.29.0.mjs',

  // Guard rails. The browser has no swap; these keep a careless filter from
  // locking up the tab.
  maxPreviewRows: 2000,
  maxNarrativeScanRows: 200000,
  maxExportRows: 100000,
};

/* ------------------------------------------------------------------ internals */

let _duckdb = null;      // the imported module namespace
let _db = null;          // AsyncDuckDB
let _conn = null;        // AsyncDuckDBConnection
let _manifest = null;
let _summary = null;
let _registered = new Set();

const listeners = new Set();

/** Subscribe to progress messages so the UI can narrate a slow first load. */
export function onProgress(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function progress(msg) { for (const fn of listeners) { try { fn(msg); } catch { /* ignore */ } } }

function fmtMB(bytes) { return `${(bytes / 1048576).toFixed(1)} MB`; }

/* ------------------------------------------------------------------ bootstrap */

/**
 * Absolute URL for a path expressed relative to the PAGE, not to this module.
 *
 * CONFIG paths are written relative to index.html because that is how they are
 * uploaded, but this file lives in assets/, and both dynamic import() and the
 * Worker constructor resolve against the module's own URL. Without this,
 * 'vendor/duckdb/…' is treated as a bare module specifier and fails with
 * "Failed to resolve module specifier".
 */
function pageUrl(path) {
  return new URL(path, document.baseURI).href;
}

async function loadDuckDbModule() {
  if (CONFIG.wasmSource === 'local') {
    return import(/* webpackIgnore: true */ pageUrl(CONFIG.localEsmEntry));
  }
  return import(
    /* webpackIgnore: true */
    `https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@${CONFIG.wasmVersion}/+esm`
  );
}

/**
 * Choose a DuckDB-WASM bundle deterministically, never the threaded one.
 *
 * duckdb.selectBundle() picks the cross-origin-isolated (COI) build whenever
 * SharedArrayBuffer is available. That build spawns pthread workers, and those
 * are fragile in exactly the environments this tool has to survive: whether a
 * page is cross-origin isolated depends on the host's COOP/COEP headers, which
 * differ between shared hosting, a local preview server and an embedded
 * browser. When the pthread workers fail to start, DuckDB does not fail
 * cleanly — it throws "RuntimeError: table index is out of bounds" from deep
 * inside the WASM module, which looks like data corruption rather than a
 * configuration problem.
 *
 * Restricting the candidate set to the single-threaded bundles removes that
 * whole class of failure. The cost is no WASM threading, which is immaterial
 * here: queries are dominated by network range requests, not CPU.
 */
async function selectBundle(duckdb) {
  if (CONFIG.wasmSource === 'local') {
    const d = CONFIG.localWasmDir;
    return duckdb.selectBundle({
      mvp: {
        mainModule: pageUrl(`${d}duckdb-mvp.wasm`),
        mainWorker: pageUrl(`${d}duckdb-browser-mvp.worker.js`),
      },
      eh: {
        mainModule: pageUrl(`${d}duckdb-eh.wasm`),
        mainWorker: pageUrl(`${d}duckdb-browser-eh.worker.js`),
      },
    });
  }
  const all = duckdb.getJsDelivrBundles();
  const singleThreaded = {};
  if (all.mvp) singleThreaded.mvp = all.mvp;
  if (all.eh) singleThreaded.eh = all.eh;
  return duckdb.selectBundle(
    Object.keys(singleThreaded).length ? singleThreaded : all);
}

/**
 * Boot DuckDB-WASM, load the manifest, and register every Parquet file as an
 * HTTP-backed virtual file. Registration is metadata only — no bytes move
 * until a query needs them.
 */
export async function init() {
  if (_conn) return { manifest: _manifest, summary: _summary };

  progress('Loading manifest…');
  const [manifest, summary] = await Promise.all([
    fetchJson(`${CONFIG.dataDir}manifest.json`),
    fetchJson(`${CONFIG.dataDir}summary.json`),
  ]);
  _manifest = manifest;
  _summary = summary;

  progress('Loading the query engine…');
  _duckdb = await loadDuckDbModule();
  const bundle = await selectBundle(_duckdb);

  /*
   * Fetch the WebAssembly module ourselves so the boot screen can report real
   * progress. This is the largest single download in the whole application —
   * about 7 MB compressed — and on a slow connection it is several seconds of
   * apparently nothing happening. Streaming it through a blob URL costs one
   * extra copy in memory but turns a blank wait into a progress bar.
   *
   * If anything about this fails we fall through to the plain URL and let
   * DuckDB fetch it itself; a missing progress bar is not worth a broken boot.
   */
  let moduleUrl = bundle.mainModule;
  let moduleBlob = null;
  try {
    const res = await fetch(bundle.mainModule);
    if (res.ok && res.body) {
      const total = Number(res.headers.get('content-length')) || 0;
      const reader = res.body.getReader();
      const chunks = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.length;
        progress(total
          ? `Loading the query engine — ${fmtMB(received)} of ${fmtMB(total)}`
          : `Loading the query engine — ${fmtMB(received)}`);
      }
      moduleBlob = URL.createObjectURL(
        new Blob(chunks, { type: 'application/wasm' }));
      moduleUrl = moduleBlob;
    }
  } catch {
    moduleUrl = bundle.mainModule;
  }

  // The worker has to be same-origin, so wrap a cross-origin script in a blob.
  let workerUrl = bundle.mainWorker;
  let revokeUrl = null;
  if (CONFIG.wasmSource !== 'local') {
    const blob = new Blob([`importScripts("${bundle.mainWorker}");`],
                          { type: 'text/javascript' });
    workerUrl = URL.createObjectURL(blob);
    revokeUrl = workerUrl;
  }

  progress('Starting the query engine…');
  const worker = new Worker(workerUrl);
  _db = new _duckdb.AsyncDuckDB(new _duckdb.VoidLogger(), worker);
  await _db.instantiate(moduleUrl, bundle.pthreadWorker);
  if (revokeUrl) URL.revokeObjectURL(revokeUrl);
  // The compiled module is retained by the engine; the transfer buffer is not.
  if (moduleBlob) URL.revokeObjectURL(moduleBlob);

  await _db.open({
    query: {
      // Cast Arrow decimals to double so JS gets plain numbers, and keep
      // large results out of a single allocation.
      castBigIntToDouble: true,
      castDecimalToDouble: true,
    },
  });
  _conn = await _db.connect();

  progress('Registering data files…');
  await registerAll();

  progress('Ready.');
  return { manifest: _manifest, summary: _summary };
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`Could not load ${url} (HTTP ${res.status}). ` +
    'Confirm the data/ folder was uploaded alongside index.html.');
  return res.json();
}

function absoluteDataUrl(file) {
  return new URL(CONFIG.dataDir + file, window.location.href).toString();
}

async function registerFile(file) {
  if (_registered.has(file)) return;
  await _db.registerFileURL(file, absoluteDataUrl(file),
                            _duckdb.DuckDBDataProtocol.HTTP, false);
  _registered.add(file);
}

/** Every Parquet file named in the manifest, flattened. */
function manifestFiles() {
  const f = _manifest.files || {};
  const out = [];
  const push = (x) => { if (x && x.file) out.push(x); };
  (f.mdr || []).forEach(push);
  Object.values(f.problems || {}).forEach(push);
  Object.values(f.dicts || {}).forEach(push);
  Object.values(f.rollups || {}).forEach(push);
  (f.foi || []).forEach(push);
  return out;
}

async function registerAll() {
  await Promise.all(manifestFiles().map((e) => registerFile(e.file)));

  // Views over the static files. mdr_all spans everything; year-scoped queries
  // go through mdrFiles() instead so unneeded years are never opened.
  const all = (_manifest.files.mdr || []).map((e) => `'${e.file}'`).join(', ');
  await run(`CREATE OR REPLACE VIEW mdr_all AS SELECT * FROM read_parquet([${all}]);`);

  const p = _manifest.files.problems || {};
  if (p.flat_dev_problems) {
    await run(`CREATE OR REPLACE VIEW devprob AS
               SELECT * FROM read_parquet('${p.flat_dev_problems.file}');`);
  }
  if (p.flat_pat_problems) {
    await run(`CREATE OR REPLACE VIEW patprob AS
               SELECT * FROM read_parquet('${p.flat_pat_problems.file}');`);
  }
  const d = _manifest.files.dicts || {};
  if (d.device_problem_dict) {
    await run(`CREATE OR REPLACE VIEW dict_device AS
               SELECT * FROM read_parquet('${d.device_problem_dict.file}');`);
  }
  if (d.patient_problem_dict) {
    await run(`CREATE OR REPLACE VIEW dict_patient AS
               SELECT * FROM read_parquet('${d.patient_problem_dict.file}');`);
  }
  for (const [name, entry] of Object.entries(_manifest.files.rollups || {})) {
    await run(`CREATE OR REPLACE VIEW ${name} AS
               SELECT * FROM read_parquet('${entry.file}');`);
  }
  if ((_manifest.files.foi || []).length) {
    const foi = _manifest.files.foi.map((e) => `'${e.file}'`).join(', ');
    await run(`CREATE OR REPLACE VIEW foi AS SELECT * FROM read_parquet([${foi}]);`);
  }
}

/* --------------------------------------------------------------- year pruning */

/**
 * The Parquet files overlapping [yearLo, yearHi]. Passing only these to
 * read_parquet() means out-of-range years are never even opened, which is the
 * difference between a 3 MB query and a 40 MB one.
 */
export function mdrFiles(yearLo, yearHi) {
  const entries = _manifest.files.mdr || [];
  const hit = entries.filter((e) => {
    const lo = e.year_min ?? -Infinity, hi = e.year_max ?? Infinity;
    return hi >= yearLo && lo <= yearHi;
  });
  return (hit.length ? hit : entries).map((e) => e.file);
}

/** A `read_parquet([...])` expression covering just the years requested. */
export function mdrSource(yearLo, yearHi) {
  const files = mdrFiles(yearLo, yearHi).map((f) => `'${f}'`).join(', ');
  return `read_parquet([${files}])`;
}

/* -------------------------------------------------------------------- queries */

/** Fire-and-forget statement (DDL, SET, …). */
export async function run(sql) {
  if (!_conn) throw new Error('Database not initialised — call init() first.');
  return _conn.query(sql);
}

/**
 * Run a SELECT and return plain JS row objects.
 *
 * Parameters are bound with a prepared statement when supplied, so user text
 * never reaches the SQL string. Identifiers and the file list are the only
 * things interpolated, and those come from the manifest, not from input.
 */
export async function query(sql, params = []) {
  if (!_conn) throw new Error('Database not initialised — call init() first.');
  let table;
  if (params && params.length) {
    const stmt = await _conn.prepare(sql);
    try {
      table = await stmt.query(...params);
    } finally {
      await stmt.close();
    }
  } else {
    table = await _conn.query(sql);
  }
  return arrowToRows(table);
}

/** First row of a query, or null. */
export async function queryRow(sql, params = []) {
  const rows = await query(sql, params);
  return rows.length ? rows[0] : null;
}

/** Single scalar value. */
export async function queryValue(sql, params = []) {
  const row = await queryRow(sql, params);
  if (!row) return null;
  const keys = Object.keys(row);
  return keys.length ? row[keys[0]] : null;
}

/**
 * Convert an Arrow table to row objects, normalising the types that trip up
 * naive consumers: BigInt counts become Numbers, Arrow dates become ISO day
 * strings, and Arrow's string views become plain strings.
 */
export function arrowToRows(table) {
  const out = [];
  for (const row of table) {
    const obj = row.toJSON ? row.toJSON() : row;
    const clean = {};
    for (const k in obj) {
      let v = obj[k];
      if (typeof v === 'bigint') {
        v = Number(v);
      } else if (v instanceof Date) {
        v = v.toISOString().slice(0, 10);
      } else if (v && typeof v === 'object' && typeof v.toString === 'function'
                 && !Array.isArray(v)) {
        // Arrow Utf8 views and Decimal scalars stringify cleanly.
        const s = v.toString();
        v = s === '[object Object]' ? v : s;
      }
      clean[k] = v;
    }
    out.push(clean);
  }
  return out;
}

/* ------------------------------------------------------------------ accessors */

export function manifest() { return _manifest; }
export function summary() { return _summary; }
export function isReady() { return !!_conn; }

/** Total bytes of the Parquet payload, for the About panel. */
export function payloadBytes() {
  return manifestFiles().reduce((acc, e) => acc + (e.bytes || 0), 0);
}

export function hasNarratives() {
  return !!(_manifest && _manifest.narratives_included);
}

export function hasFoi() {
  return !!(_manifest && (_manifest.files.foi || []).length);
}

/**
 * Reports structurally eligible to carry a problem code.
 *
 * The FDA device/foitext/problem-code source files only cover 2015 onward,
 * while the MDR master file reaches back to 1991. Roughly 20% of the corpus
 * therefore CANNOT carry a problem code, and including those rows in the
 * disproportionality comparator inflates every PRR by ~25%. The comparator
 * uses this figure instead of the raw corpus total.
 */
export const CODE_ELIGIBLE_FROM_YEAR = 2015;

let _eligibleTotal = null;
export async function codeEligibleTotal() {
  if (_eligibleTotal !== null) return _eligibleTotal;
  /*
   * Published by the exporter in summary.json. Computing it in the browser
   * means counting 20.7 million rows across every year file — 717 ms measured,
   * sitting on the critical path of the slowest panel — to obtain a number
   * that does not change between data releases. The query remains as a
   * fallback so an older data/ folder still works.
   */
  if (_summary && Number.isFinite(_summary.code_eligible_reports)) {
    _eligibleTotal = Number(_summary.code_eligible_reports);
    return _eligibleTotal;
  }
  _eligibleTotal = Number(await queryValue(
    `SELECT COUNT(*) FROM mdr_all
      WHERE report_year IS NULL OR report_year >= ${CODE_ELIGIBLE_FROM_YEAR}`));
  return _eligibleTotal;
}
