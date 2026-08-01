/* Applies the stored theme, for pages that are not the app.
 *
 * index.html gets this for free inside app.js, which also wires the toggle.
 * privacy.html has no app.js and no toggle, but it does have a visitor who may
 * have chosen dark - and a static page that ignores that choice flashes white
 * on the way in, which is exactly the sort of small betrayal a page about
 * respecting the visitor should not commit.
 *
 * Deliberately a classic script, not a module: modules defer, and a deferred
 * theme runs after first paint, which is the flash this exists to prevent.
 * Loaded from the same origin like everything else, so the CSP allows it.
 *
 * Reads localStorage and writes nothing. The key and its three values are the
 * ones app.js owns; if they ever change, they change in both places.
 */
(function () {
  try {
    var theme = localStorage.getItem("theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.dataset.theme = theme;
    }
    // "auto" and a missing key are the same instruction: leave the root alone
    // and let the color-scheme:light dark in style.css follow the system.
  } catch (err) {
    // Storage can throw outright in a locked-down browser or a sandboxed
    // frame. The page is entirely readable in the system theme, so there is
    // nothing to recover and nothing worth reporting.
  }
})();
