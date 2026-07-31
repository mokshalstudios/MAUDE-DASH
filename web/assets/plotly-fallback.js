/*
 * plotly-fallback.js — load Plotly from the CDN only if the vendored copy is absent.
 *
 * The tool ships expecting web/vendor/plotly/plotly.min.js (see
 * packaging/vendor_assets.py). If that file was not uploaded, this pulls the
 * same pinned version from jsDelivr so charts still render.
 *
 * document.write is used deliberately: it executes during parsing, so Plotly is
 * defined before app.js runs. This file exists separately rather than inline so
 * the Content-Security-Policy can forbid inline script entirely.
 */
if (typeof Plotly === 'undefined') {
  document.write(
    '<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@3.0.1/plotly.min.js">' +
    '\x3C/script>');
}
