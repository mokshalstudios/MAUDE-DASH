/*
 * site.js — behaviour shared by the landing page and the documentation pages.
 *
 * Everything here is progressive enhancement: the pages are fully readable and
 * navigable with JavaScript disabled. Extracted from inline <script> blocks so
 * the Content-Security-Policy can disallow inline script.
 */
(function () {
  'use strict';

  /* Copyright year. */
  var y = document.getElementById('year');
  if (y) y.textContent = new Date().getFullYear();

  /* Mobile navigation. */
  var toggle = document.getElementById('nav-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var nav = document.querySelector('.site-nav');
      if (!nav) return;
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
    });
  }

  /* Copy-citation button. */
  var copy = document.getElementById('copy-cite');
  if (copy) {
    copy.addEventListener('click', function () {
      var text = document.getElementById('cite-text');
      if (!text || !navigator.clipboard) { copy.textContent = 'Copy unavailable'; return; }
      navigator.clipboard.writeText(text.textContent)
        .then(function () { copy.textContent = 'Copied'; })
        .catch(function () { copy.textContent = 'Copy failed'; });
      setTimeout(function () { copy.textContent = 'Copy citation'; }, 1600);
    });
  }

  /*
   * Headline figures are read from the same summary.json the tool uses, so the
   * landing page can never drift from the data actually shipped. The markup
   * carries sensible values already, so a failed fetch changes nothing.
   */
  var stats = document.getElementById('hero-stats');
  if (!stats) return;

  fetch('data/summary.json', { cache: 'no-cache' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (s) {
      if (!s) return;
      var fmt = function (n) { return Number(n).toLocaleString('en-US'); };
      var rows = [
        ['Reports', fmt(s.total_reports)],
        ['Years', s.year_min + '–' + s.year_max],
        ['Product codes', fmt(s.product_codes)],
        ['Manufacturers', fmt(s.manufacturers)]
      ];
      stats.textContent = '';
      rows.forEach(function (r) {
        var wrap = document.createElement('div');
        var dt = document.createElement('dt');
        var dd = document.createElement('dd');
        dt.textContent = r[0];
        dd.textContent = r[1];
        wrap.appendChild(dt);
        wrap.appendChild(dd);
        stats.appendChild(wrap);
      });

      if (s.data_vintage) {
        ['vintage-inline', 'vintage-footer'].forEach(function (id) {
          var n = document.getElementById(id);
          if (n) n.textContent = s.data_vintage;
        });
        var cite = document.getElementById('cite-text');
        if (cite) {
          cite.textContent = cite.textContent.replace(
            /current to [^)]*\)/, 'current to ' + s.data_vintage + ')');
        }
      }
    })
    .catch(function () { /* landing page stands on its own */ });
})();
