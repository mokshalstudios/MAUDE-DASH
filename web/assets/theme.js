/*
 * theme.js — set the colour theme before first paint.
 *
 * Must be loaded as a plain synchronous <script src> in <head>, with no defer
 * or async, so it runs before the body renders and there is no flash of the
 * wrong theme. It exists as a file rather than an inline block purely so the
 * Content-Security-Policy can forbid inline script entirely.
 */
(function () {
  var t = null;
  try { t = localStorage.getItem('maudedash-theme'); } catch (e) { /* private mode */ }
  if (t !== 'light' && t !== 'dark') {
    t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  document.documentElement.setAttribute('data-theme', t);
})();
