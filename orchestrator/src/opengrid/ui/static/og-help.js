/* The Help page's search-within-page filter and table-of-contents highlight (/og/help, r3.4).
 * The filter hides whole topics (.hp-sub) that do not contain every search word, and within a matching
 * topic hides the table rows that do not (unless the topic's heading itself matches). Esc clears it.
 */
(function () {
  "use strict";

  const input = document.getElementById("hp-search-input");
  const body = document.getElementById("hp-body");
  const status = document.getElementById("hp-search-status");
  const noMatch = document.getElementById("hp-no-match");
  if (!input || !body) {
    return;
  }
  const HIDDEN = "hp-filtered";
  const sections = Array.from(body.querySelectorAll(".hp-sec"));
  const subs = Array.from(body.querySelectorAll(".hp-sub"));
  const tocLinks = Array.from(document.querySelectorAll(".hp-toc a[href^='#']"));
  const textOf = new Map();

  function text(el) {
    if (!textOf.has(el)) {
      textOf.set(el, (el.textContent || "").toLowerCase());
    }
    return textOf.get(el);
  }

  function matches(haystack, terms) {
    return terms.every(function (t) { return haystack.indexOf(t) !== -1; });
  }

  function clear() {
    body.querySelectorAll("." + HIDDEN).forEach(function (el) { el.classList.remove(HIDDEN); });
    tocLinks.forEach(function (a) { a.parentElement.classList.remove("hp-hidden"); });
    if (noMatch) { noMatch.hidden = true; }
    if (status) { status.textContent = ""; }
  }

  function apply() {
    const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    clear();
    if (!terms.length) {
      return;
    }
    let shown = 0;
    subs.forEach(function (sub) {
      if (!matches(text(sub), terms)) {
        sub.classList.add(HIDDEN);
        return;
      }
      shown += 1;
      const heading = sub.querySelector("h3");
      if (heading && matches(text(heading), terms)) {
        return;
      }
      sub.querySelectorAll(".hp-table tbody tr").forEach(function (row) {
        if (!matches(text(row), terms)) {
          row.classList.add(HIDDEN);
        }
      });
    });
    sections.forEach(function (sec) {
      const visibleSub = sec.querySelector(".hp-sub:not(." + HIDDEN + ")");
      if (!visibleSub && !matches(text(sec), terms)) {
        sec.classList.add(HIDDEN);
      } else if (!visibleSub) {
        shown += 1;
      }
    });
    tocLinks.forEach(function (a) {
      const target = document.getElementById(a.getAttribute("href").slice(1));
      const hidden = !target || target.classList.contains(HIDDEN) || !!target.closest("." + HIDDEN);
      a.parentElement.classList.toggle("hp-hidden", hidden);
    });
    if (noMatch) { noMatch.hidden = shown > 0; }
    if (status) { status.textContent = shown ? shown + (shown === 1 ? " topic matches" : " topics match") : "No matches"; }
  }

  let timer = null;
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(apply, 120);
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      input.value = "";
      apply();
    }
  });

  function markCurrent() {
    const id = decodeURIComponent(window.location.hash.slice(1));
    tocLinks.forEach(function (a) { a.classList.toggle("hp-current", a.getAttribute("href") === "#" + id); });
  }
  window.addEventListener("hashchange", markCurrent);
  markCurrent();
})();
