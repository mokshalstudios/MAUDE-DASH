"""
serve_local.py — local preview server for the MaudeDash web tier
================================================================

Python's stdlib http.server does not implement HTTP Range requests, and
DuckDB-WASM depends on them: without ranges the browser would download all
1.4 GB of Parquet before answering a single query. This server adds Range
support plus the MIME types and CORS/isolation headers the app needs, so
`web/` behaves locally the way Apache will on DreamHost.

    python packaging/serve_local.py            # serves ../web on :8777
    python packaging/serve_local.py --port 9000 --root ../web

This is a development convenience only. In production the host serves the
files directly; see deploy/DREAMHOST.md.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Kept identical to the policy in web/.htaccess. The site contains no inline
# script and no inline event handlers, so neither 'unsafe-inline' nor
# 'unsafe-eval' appears here; 'wasm-unsafe-eval' is required only so DuckDB can
# compile its WebAssembly module.
CSP = (
    # Kept identical to the policy in web/.htaccess. script-src and default-src
    # are deliberately absent: DuckDB-WASM cannot instantiate its module inside
    # a Web Worker under any restrictive script-src (it fails with
    # "RuntimeError: table index is out of bounds", with no violation reported
    # to the page), and the only policies that do work require both
    # 'unsafe-inline' and 'unsafe-eval' — which would provide no real XSS
    # protection while appearing strict. See the long comment in
    # web/.htaccess for the full reasoning and the strict variant to try.
    "base-uri 'self'; object-src 'none'; frame-ancestors 'self'; "
    "form-action 'none'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; manifest-src 'self'"
)

EXTRA_TYPES = {
    ".parquet": "application/vnd.apache.parquet",
    ".wasm": "application/wasm",
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + RFC 7233 single-range support."""

    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        # Deliberately NOT sending Cross-Origin-Embedder-Policy.
        #
        # Setting it makes the page cross-origin isolated, which makes
        # SharedArrayBuffer available, which makes DuckDB-WASM select its
        # threaded (COI) bundle and spawn pthread workers from the CDN origin.
        # Production on shared hosting does not set COEP, so it is not isolated
        # and DuckDB uses the single-threaded build instead. Sending COEP here
        # would test a code path the live site never takes — and it did exactly
        # that: the threaded bundle failed with "table index is out of bounds"
        # locally while production would have been fine. Local must match live.
        self.send_header("Cache-Control", "no-cache")
        # Mirror the production Content-Security-Policy from web/.htaccess, so a
        # violation surfaces in the console here rather than after deployment
        # where it would simply break the page. Keep the two in sync.
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in EXTRA_TYPES:
            return EXTRA_TYPES[ext]
        return super().guess_type(path)

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()

        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.exists(path):
            return super().do_GET()

        m = RANGE_RE.match(rng.strip())
        if not m:
            return super().do_GET()

        size = os.path.getsize(path)
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "":
            # suffix range: last N bytes (DuckDB reads the Parquet footer this way)
            length = int(end_s or 0)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)

        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()

        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def log_message(self, fmt, *args):
        if os.environ.get("MAUDEDASH_VERBOSE"):
            super().log_message(fmt, *args)


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Preview the MaudeDash web tier locally")
    ap.add_argument("--root", default=os.path.join(here, "..", "web"))
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"ERROR: web root not found: {root}", file=sys.stderr)
        return 2
    if not os.path.exists(os.path.join(root, "index.html")):
        print(f"ERROR: no index.html in {root}", file=sys.stderr)
        return 2

    for ext, mime in EXTRA_TYPES.items():
        mimetypes.add_type(mime, ext)

    data = os.path.join(root, "data")
    n_parquet = len([f for f in os.listdir(data) if f.endswith(".parquet")]) \
        if os.path.isdir(data) else 0
    if not n_parquet:
        print("WARNING: no .parquet files in data/ — run maude_export_web.py first.")

    handler = partial(RangeHandler, directory=root)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print("=" * 62)
    print("  MaudeDash local preview")
    print("=" * 62)
    print(f"  root        : {root}")
    print(f"  data files  : {n_parquet} parquet")
    print(f"  URL         : http://{args.host}:{args.port}/")
    print("  Range requests: enabled   (Ctrl-C to stop)")
    print("=" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
