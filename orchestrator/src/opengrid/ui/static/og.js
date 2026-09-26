/* opengrid.ui JS helpers (BUILD.md UI brief base contract): `og.sse(url, handler)` and
 * `og.chart(el, option)`. No build step -- loaded as a plain script from CDN-free static/. Owner: ui-a.
 */
(function () {
  "use strict";

  const og = window.og || {};

  /**
   * Subscribe to a server-sent-events endpoint and call `handler(data, event)` for every message.
   * `: keepalive` comment frames (02b S7.2) are ignored automatically -- they never fire `onmessage`.
   * The browser's native EventSource reconnect handles drops; `onSseError` (optional) is called on
   * every transport error so a screen can show a "reconnecting" indicator.
   *
   * @param {string} url
   * @param {(data: any, event: MessageEvent) => void} handler
   * @param {{onError?: (event: Event) => void, onOpen?: () => void}} [opts]
   * @returns {EventSource}
   */
  og.sse = function sse(url, handler, opts) {
    opts = opts || {};
    const source = new EventSource(url);
    source.onmessage = function (event) {
      let data = event.data;
      try {
        data = JSON.parse(event.data);
      } catch (_err) {
        /* non-JSON payload: hand the raw string to the caller */
      }
      handler(data, event);
    };
    if (opts.onOpen) {
      source.onopen = opts.onOpen;
    }
    source.onerror = function (event) {
      if (opts.onError) {
        opts.onError(event);
      }
    };
    return source;
  };

  /**
   * Initialize (or re-use) an ECharts instance on `el` and apply `option`. Resizes on window resize.
   * @param {string | Element} el
   * @param {object} option
   * @returns {import("echarts").ECharts}
   */
  og.chart = function chart(el, option) {
    const node = typeof el === "string" ? document.querySelector(el) : el;
    if (!node) {
      throw new Error("og.chart: element not found: " + el);
    }
    const existing = window.echarts.getInstanceByDom(node);
    const instance = existing || window.echarts.init(node);
    instance.setOption(option, true);
    // A chart initialised from an inline <script> mid-parse (markets_series_fragment.html) measures its
    // container before the surrounding grid has laid out its columns, so the canvas came out row-wide
    // and drew over the neighbouring tiles (seen live, U1). Re-measure once layout has settled.
    window.requestAnimationFrame(function () {
      instance.resize();
    });
    window.addEventListener("resize", function () {
      instance.resize();
    });
    return instance;
  };

  /** Format an age in seconds as a short human string, e.g. "3s", "2m", "1h". */
  og.formatAge = function formatAge(ageS) {
    if (ageS === null || ageS === undefined || Number.isNaN(ageS)) {
      return "unknown";
    }
    if (ageS < 60) {
      return Math.round(ageS) + "s";
    }
    if (ageS < 3600) {
      return Math.round(ageS / 60) + "m";
    }
    return Math.round(ageS / 3600) + "h";
  };

  /**
   * Every `[data-since]` element (an ISO-8601 instant) is re-rendered every second as "age: Ns" via
   * `.stale-badge` text, and flips `data-stale="true"` past `data-stale-after` seconds -- the live
   * counterpart to the server-rendered `stale_badge.html` partial (BUILD.md UI brief: "every value
   * shows its age/staleness").
   */
  og.initStaleBadges = function initStaleBadges(root) {
    root = root || document;
    function tick() {
      const now = Date.now();
      root.querySelectorAll("[data-since]").forEach(function (elem) {
        const since = Date.parse(elem.getAttribute("data-since"));
        if (Number.isNaN(since)) {
          return;
        }
        const ageS = (now - since) / 1000;
        const thresholdRaw = elem.getAttribute("data-stale-after");
        const threshold = thresholdRaw ? parseFloat(thresholdRaw) : null;
        elem.textContent = "age: " + og.formatAge(ageS);
        if (threshold !== null) {
          elem.setAttribute("data-stale", ageS > threshold ? "true" : "false");
        }
      });
    }
    tick();
    return window.setInterval(tick, 1000);
  };

  /** Toggle/persist the light/dark theme (`data-theme` on <html>), per-viewer only. */
  og.toggleTheme = function toggleTheme() {
    const root = document.documentElement;
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try {
      window.localStorage.setItem("og-theme", next);
    } catch (_err) {
      /* private mode / blocked storage: theme just won't persist across reloads */
    }
  };

  (function restoreTheme() {
    try {
      const saved = window.localStorage.getItem("og-theme");
      if (saved) {
        document.documentElement.setAttribute("data-theme", saved);
      }
    } catch (_err) {
      /* ignore */
    }
  })();

  window.og = og;

  document.addEventListener("DOMContentLoaded", function () {
    og.initStaleBadges();
  });
})();
