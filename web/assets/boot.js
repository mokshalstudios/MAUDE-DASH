/*
 * boot.js — landing-page entry behaviour, loaded synchronously in <head>.
 *
 * Deep links of the form /?pc=KWP&y0=2018 predate the landing page: they were
 * shared, bookmarked and potentially cited while the tool lived at the site
 * root. Forwarding them keeps every one of those URLs working.
 *
 * replace() rather than assign() so the redirect does not add a history entry
 * and trap the back button.
 */
(function () {
  if (window.location.search && window.location.search.length > 1) {
    window.location.replace('app.html' + window.location.search + window.location.hash);
  }
})();
