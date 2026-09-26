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
  /** Read a CSS custom property from :root, optionally as an rgba with the given alpha. */
  og.token = function token(name, alpha) {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (alpha === undefined || !/^#[0-9a-f]{6}$/i.test(raw)) {
      return raw;
    }
    const n = parseInt(raw.slice(1), 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
  };

  /**
   * One ECharts theme built from the live tokens (registered once per data-theme): smooth 2px lines
   * with no symbols, hidden axis lines, soft dashed grid lines, muted axis labels, a panel-coloured
   * tooltip card. Every chart on every screen inherits it, so screens only pass data.
   */
  og.chartTheme = function chartTheme() {
    const name = "og-" + (document.documentElement.getAttribute("data-theme") || "dark");
    if (!og._themes) {
      og._themes = {};
    }
    if (!og._themes[name]) {
      const muted = og.token("--muted");
      const grid = og.token("--border", 0.6);
      // ISO instants ("2026-09-25T17:45:00+00:00") are shown as HH:MM on the axis; the tooltip keeps the
      // full value. `hideOverlap` drops labels that would collide instead of clipping them.
      const isoTime = /^\d{4}-\d\d-\d\dT\d\d:\d\d/;
      const axisCommon = {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: muted, fontSize: 11, fontFamily: "Geist, Inter, system-ui, sans-serif", hideOverlap: true,
          formatter: function (v) { return isoTime.test(String(v)) ? String(v).slice(11, 16) : v; },
        },
        splitLine: { show: true, lineStyle: { color: grid, type: [4, 6] } },
        nameTextStyle: { color: muted, fontSize: 11 },
      };
      window.echarts.registerTheme(name, {
        color: [
          og.token("--accent"), og.token("--series-ercot-energy"), og.token("--series-ercot-as"),
          og.token("--series-dist-deferral"), og.token("--series-large-load"), og.token("--series-pipeline-ac"),
          og.token("--series-partner-capacity"), og.token("--series-pjm-capacity"),
        ],
        backgroundColor: "transparent",
        textStyle: { color: muted, fontFamily: "Geist, Inter, system-ui, sans-serif" },
        legend: { textStyle: { color: muted, fontSize: 11 }, itemWidth: 10, itemHeight: 10, icon: "circle" },
        tooltip: {
          backgroundColor: og.token("--panel"),
          borderColor: og.token("--border"),
          borderWidth: 1,
          padding: [8, 12],
          textStyle: { color: og.token("--text"), fontSize: 12 },
          extraCssText: "border-radius: 10px; box-shadow: 0 12px 32px -16px rgba(0,0,0,.6);",
        },
        categoryAxis: Object.assign({}, axisCommon, { splitLine: { show: false } }),
        valueAxis: axisCommon,
        timeAxis: axisCommon,
        line: { smooth: 0.35, symbol: "none", lineStyle: { width: 2 } },
        bar: { barMaxWidth: 56, itemStyle: { borderRadius: [4, 4, 0, 0] } },
      });
      og._themes[name] = true;
    }
    return name;
  };

  og.chart = function chart(el, option) {
    const node = typeof el === "string" ? document.querySelector(el) : el;
    if (!node) {
      throw new Error("og.chart: element not found: " + el);
    }
    const existing = window.echarts.getInstanceByDom(node);
    const instance = existing || window.echarts.init(node, og.chartTheme());
    // A single line series gets a soft area fill under it (reference: one accent line over a dark
    // card); multi-line charts stay clean so the zones remain distinguishable.
    // Screens may reference tokens as "token:--name" or "token:--name@0.2" in any colour slot; resolve
    // them here so python view code never carries a hex that belongs to og.css.
    (function resolveTokens(node) {
      if (Array.isArray(node)) { node.forEach(resolveTokens); return; }
      if (!node || typeof node !== "object") { return; }
      Object.keys(node).forEach(function (key) {
        const v = node[key];
        if (typeof v === "string" && v.indexOf("token:") === 0) {
          const parts = v.slice(6).split("@");
          node[key] = og.token(parts[0], parts[1] !== undefined ? parseFloat(parts[1]) : undefined);
        } else {
          resolveTokens(v);
        }
      });
    })(option);
    // "token:kw" in a tooltip valueFormatter slot -> a kW formatter (python view code stays free of JS).
    if (option.tooltip && option.tooltip.valueFormatter === "token:kw") {
      option.tooltip.valueFormatter = function (v) { return (typeof v === "number" ? v.toFixed(1) : v) + " kW"; };
    }
    // An empty chart says so instead of drawing a bare axis frame and a stray legend.
    const hasData = (option.series || []).some(function (s) { return (s.data || []).length > 0; });
    if (!hasData) {
      option.legend = { show: false };
      option.xAxis = { show: false };
      option.yAxis = { show: false };
      option.title = {
        text: node.dataset.empty || "No data yet", left: "center", top: "middle",
        textStyle: { color: og.token("--muted"), fontSize: 13, fontWeight: 400, width: Math.max(node.clientWidth - 48, 200), overflow: "break" },
      };
    } else if (!option.grid) {
      option.grid = { containLabel: true, left: 8, right: 12, top: 28, bottom: 8 };
    }
    // Respect the OS reduced-motion preference for canvas animation too (CSS cannot reach it).
    if (og.reducedMotion()) {
      option.animation = false;
    }
    const lines = (option.series || []).filter(function (s) { return s.type === "line"; });
    if (lines.length === 1 && !lines[0].areaStyle) {
      lines[0].areaStyle = {
        color: {
          type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: og.token("--accent", 0.28) },
            { offset: 1, color: og.token("--accent", 0) },
          ],
        },
      };
    }
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

  /**
   * Degraded-mode banner (Health, Control room) from a stream frame's `degraded_modes`. The labels come from
   * the server (`opengrid.ui.routes.health.DEGRADED_MODE_LABELS`, the element's data-labels); an unknown
   * mode shows its code. A frame without the field leaves the banner as it is.
   */
  og.renderDegradedBanner = function renderDegradedBanner(el, modes) {
    if (!el || !Array.isArray(modes)) {
      return;
    }
    var labels = JSON.parse(el.dataset.labels || "{}");
    var text = modes.map(function (m) { return labels[m] || m; }).join(" + ");
    el.hidden = !text;
    el.textContent = text ? "Degraded mode: " + text : "";
  };

  /** True when the viewer asked the OS for reduced motion. */
  og.reducedMotion = function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
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
    if (ageS < 172800) {
      return Math.round(ageS / 3600) + "h";
    }
    return Math.round(ageS / 86400) + "d";
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
        // A forecast product's latest value is timestamped in the future (day-ahead prices, wind and
        // solar forecasts); show how far ahead it reaches rather than a negative age (seen live).
        if (ageS < 0) {
          elem.textContent = "ahead: " + og.formatAge(-ageS);
          elem.setAttribute("data-stale", "false");
          return;
        }
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
    // The settle-in animation is a first-paint moment only: once the page has landed, mark the
    // document so sections swapped in later (the 30 s self-polling screens) appear without replaying it.
    window.setTimeout(function () {
      document.documentElement.classList.add("og-settled");
    }, 600);
  });
})();
