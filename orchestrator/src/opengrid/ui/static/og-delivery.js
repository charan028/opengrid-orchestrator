/* D-38 measured delivery on the Dispatch page: a Delivery cell per deployed call and a detail drawer.
   Reads GET <base>/api/delivery/records?call_ids=... and /api/delivery/records/<id> (viewer role).
   kW in the API is +charge/-discharge; this view shows discharge as positive. */
(function () {
  "use strict";
  var script = document.currentScript;
  var basePath = (script && script.dataset.basePath) || "/og";
  var BADGE = { PASS: "status-good", PARTIAL: "status-caution", FAIL: "status-danger", IN_PROGRESS: "status-info" };

  function text(value, digits) {
    return value === null || value === undefined ? "--" : Number(value).toFixed(digits || 0);
  }
  function discharge(kw) {
    return kw === null || kw === undefined ? null : -Number(kw);
  }
  function badge(result, meter) {
    var span = document.createElement("span");
    span.className = "status-badge " + (BADGE[result] || "status-info");
    span.textContent = result;
    var wrap = document.createElement("span");
    wrap.appendChild(span);
    if (meter === "UNCORROBORATED") {
      var m = document.createElement("span");
      m.className = "status-badge status-caution";
      m.textContent = "METER";
      m.title = "The independent meter disagrees with battery telemetry";
      wrap.appendChild(document.createTextNode(" "));
      wrap.appendChild(m);
    }
    return wrap;
  }
  function fill(cell, record) {
    cell.textContent = "";
    if (!record) {
      cell.textContent = "pending";
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-link delivery-open";
    button.dataset.call = record.call_id;
    button.appendChild(badge(record.result, record.meter_status));
    button.appendChild(
      document.createTextNode(" " + text(discharge(record.delivered_kw_last)) + " / " + text(-record.committed_kw) + " kW")
    );
    button.addEventListener("click", function () { openDrawer(record.call_id); });
    cell.appendChild(button);
  }
  function refresh() {
    var cells = document.querySelectorAll("[data-delivery-call]");
    if (!cells.length) { return; }
    var ids = Array.prototype.map.call(cells, function (c) { return c.dataset.deliveryCall; });
    fetch(basePath + "/api/delivery/records?call_ids=" + encodeURIComponent(ids.join(",")), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (rows) {
        var byId = {};
        rows.forEach(function (row) { byId[row.call_id] = row; });
        Array.prototype.forEach.call(cells, function (c) { fill(c, byId[c.dataset.deliveryCall]); });
      })
      .catch(function () { /* the cells keep their last value; the next refresh retries */ });
  }
  function fact(list, label, value) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    list.appendChild(dt);
    list.appendChild(dd);
  }
  function openDrawer(callId) {
    var drawer = document.getElementById("delivery-drawer");
    if (!drawer) { return; }
    fetch(basePath + "/api/delivery/records/" + encodeURIComponent(callId), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (rec) {
        if (!rec) { return; }
        var facts = document.getElementById("delivery-drawer-facts");
        facts.textContent = "";
        fact(facts, "Result", rec.result + (rec.reasons.length ? " (" + rec.reasons.join(", ") + ")" : ""));
        fact(facts, "Committed", text(-rec.committed_kw) + " kW");
        fact(facts, "Time to target", rec.time_to_target_s === null ? "not reached" : text(rec.time_to_target_s) + " s (ramp " + text(rec.ramp_time_s) + " s)");
        fact(facts, "Sustained", rec.sustained_pct === null ? "--" : text(rec.sustained_pct, 1) + " %");
        fact(facts, "Lowest", text(discharge(rec.lowest_kw)) + " kW for " + text(rec.lowest_run_s) + " s");
        fact(facts, "Energy", text(rec.discharged_kwh, 1) + " / " + text(rec.committed_kwh, 1) + " kWh");
        fact(facts, "Meter", rec.meter_status + (rec.meter_mismatch_frac === null ? "" : " (" + text(100 * rec.meter_mismatch_frac, 1) + " % apart)"));
        drawer.hidden = false;
        chart(rec);
      });
  }
  function chart(rec) {
    var el = document.getElementById("delivery-drawer-chart");
    if (!el || typeof echarts === "undefined") { return; }
    var t = rec.series.map(function (p) { return p.t; });
    function line(name, key) {
      return { name: name, type: "line", showSymbol: false, data: rec.series.map(function (p) { return discharge(p[key]); }) };
    }
    var inst = echarts.getInstanceByDom(el) || echarts.init(el);
    inst.setOption({
      tooltip: { trigger: "axis" },
      legend: { top: 0 },
      grid: { left: 56, right: 16, top: 32, bottom: 32 },
      xAxis: { type: "category", data: t, axisLabel: { formatter: function (v) { return v.slice(11, 19); } } },
      yAxis: { type: "value", name: "kW (discharge +)" },
      series: [line("Committed", "c"), line("Commanded", "m"), line("Delivered", "d"), line("Meter change", "md")]
    }, true);
  }
  document.addEventListener("DOMContentLoaded", function () {
    var close = document.getElementById("delivery-drawer-close");
    if (close) {
      close.addEventListener("click", function () { document.getElementById("delivery-drawer").hidden = true; });
    }
    refresh();
    window.setInterval(refresh, 15000);
  });
})();
