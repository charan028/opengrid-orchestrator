/* Fleet screen (owner review R3): selection shared by the table, the map and "select all matching";
   typeahead comboboxes for every id field; the hub detail drawer; inline validation on the action cards.
   Server-side state (filters, sort, page) lives in the URL query, rendered by opengrid.ui.routes.fleet. */
(function () {
  "use strict";

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  // ---- typeahead combobox ---------------------------------------------------------------------
  function combobox(input, basePath) {
    var list = document.getElementById(input.getAttribute("aria-controls"));
    var items = [];
    var active = -1;
    var timer = null;
    var seq = 0;

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }
    function highlight(i) {
      var opts = list.querySelectorAll("[role=option]");
      opts.forEach(function (o, k) { o.setAttribute("aria-selected", k === i ? "true" : "false"); });
      active = i;
      if (opts[i]) {
        input.setAttribute("aria-activedescendant", opts[i].id);
        opts[i].scrollIntoView({ block: "nearest" });
      }
    }
    function pick(i) {
      var item = items[i];
      if (!item) { return; }
      input.value = item.id;
      var fill = input.dataset.fillBank && document.querySelector(input.dataset.fillBank);
      if (fill && item.bank_id) { fill.value = item.bank_id; }
      var limits = input.dataset.limits && document.querySelector(input.dataset.limits);
      if (limits && item.rated_p_kw) {
        limits.min = String(-item.rated_p_kw);
        limits.max = String(item.rated_p_kw);
        limits.dataset.hubLimit = String(item.rated_p_kw);
        var hint = document.getElementById(limits.getAttribute("aria-describedby"));
        if (hint) {
          hint.textContent = "Between " + (-item.rated_p_kw) + " and " + item.rated_p_kw +
            " kW for " + item.id + " (+ discharges, − charges).";
        }
      }
      close();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function render() {
      list.innerHTML = "";
      items.forEach(function (item, i) {
        var li = document.createElement("li");
        li.id = input.id + "-opt-" + i;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.dataset.value = item.id;
        var main = document.createElement("span");
        main.className = "fl-opt-id";
        main.textContent = item.id;
        li.appendChild(main);
        var meta = [item.zone, item.health_label].filter(Boolean).join(" · ");
        if (meta) {
          var m = document.createElement("span");
          m.className = "fl-opt-meta";
          m.textContent = meta;
          li.appendChild(m);
        }
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(i); });
        list.appendChild(li);
      });
      if (!items.length) {
        var none = document.createElement("li");
        none.className = "fl-opt-none";
        none.textContent = "No matches";
        list.appendChild(none);
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }
    function search() {
      var kind = input.dataset.combobox;
      var mine = ++seq;
      fetch(basePath + "/fleet/search?kind=" + encodeURIComponent(kind) + "&q=" +
            encodeURIComponent(input.value.trim()) + "&limit=20", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .then(function (body) {
          if (mine !== seq || document.activeElement !== input) { return; }
          items = (body && body.items) || [];
          render();
        })
        .catch(function () { items = []; });
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(search, 150);
    });
    input.addEventListener("focus", function () { if (!input.disabled) { search(); } });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (list.hidden) { search(); return; }
        highlight(Math.min(active + 1, items.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        highlight(Math.max(active - 1, 0));
      } else if (e.key === "Enter" && !list.hidden && active >= 0) {
        e.preventDefault();
        pick(active);
      } else if (e.key === "Escape" && !list.hidden) {
        e.preventDefault();
        e.stopPropagation();
        close();
      }
    });
  }

  // ---- scope select drives the scope-id combobox ------------------------------------------------
  function scopeLink(select) {
    var target = document.querySelector(select.dataset.scopeFor);
    if (!target) { return; }
    var hint = document.getElementById(target.getAttribute("aria-describedby") || "");
    function sync() {
      var scope = select.value;
      var fleet = scope === "fleet";
      target.disabled = fleet;
      target.required = !fleet;
      if (fleet) { target.value = ""; }
      target.dataset.combobox = scope === "bank" ? "bank" : "zone";
      target.placeholder = fleet ? "not needed for Fleet" : "type a " + scope + " id";
      if (hint) {
        hint.textContent = fleet ? "Not needed for Fleet scope." : "Pick the " + scope + " to " +
          (select.id === "rel-scope" ? "release." : "stop.");
      }
      target.dispatchEvent(new Event("change", { bubbles: true }));
    }
    select.addEventListener("change", sync);
    sync();
  }

  // ---- inline validation: disabled with a reason -----------------------------------------------
  function validation(form) {
    var button = form.querySelector(".fl-submit");
    var why = form.querySelector(".fl-why");
    if (!button || button.id === "bulk-propose-button" || button.id === "release-review-button") { return; }
    function check() {
      var problems = [];
      var hub = form.querySelector("[name=hub_id]");
      var bank = form.querySelector("[name=bank_id]");
      if (button.dataset.requires === "target" && hub && bank && !hub.value.trim() && !bank.value.trim()) {
        problems.push("Pick a hub or a bank.");
      }
      var setpoint = form.querySelector("[name=p_kw_setpoint]");
      if (setpoint) {
        var raw = setpoint.value.trim();
        var limit = parseFloat(setpoint.dataset.hubLimit || "");
        if (raw === "") {
          problems.push("Enter a setpoint.");
        } else if (!isNaN(limit) && Math.abs(parseFloat(raw)) > limit) {
          problems.push("Setpoint is outside this hub's ±" + limit + " kW.");
        }
      }
      var scopeId = form.querySelector("[name=scope_id]");
      if (scopeId && !scopeId.disabled && !scopeId.value.trim()) { problems.push("Pick a scope id."); }
      var reason = form.querySelector("[name=reason]");
      if (reason && !reason.value.trim()) { problems.push("A reason is required."); }
      button.disabled = problems.length > 0;
      button.title = problems.join(" ");
      if (why) { why.textContent = problems.length ? "To propose: " + problems.join(" ") : ""; }
    }
    form.addEventListener("input", check);
    form.addEventListener("change", check);
    check();
    form.ogCheck = check;
  }

  // ---- the page -------------------------------------------------------------------------------
  function init(opts) {
    var basePath = opts.basePath;
    var mapEl = document.getElementById("fleet-map");
    var hubs = JSON.parse(mapEl.dataset.hubs || "[]");
    var handle = og.map.create(mapEl, { center: [31.0, -99.0], zoom: 6 });
    og.map.addHubLayer(handle, hubs, { colorBy: "health" });
    og.map.addLegend(
      handle,
      [{ heading: "Hub health" }].concat(og.map.hubLegendItems("health"), [
        { heading: "Selection" },
        { label: "selected for a command", color: og.token("--accent") },
      ]),
      og.map.derivedPositions(hubs)
        ? "Hubs are placed inside their real load zone; exact coordinates arrive with /og/api/fleet/map."
        : null
    );

    // One selection, three ways in: row checkboxes, the map, and "select all matching".
    var selection = [];
    var idsField = document.getElementById("bulk-hub-ids");
    var countEl = document.getElementById("fleet-selection-count");
    var noteEl = document.getElementById("fleet-selection-note");
    var proposeButton = document.getElementById("bulk-propose-button");
    var selectionNote = document.getElementById("bulk-selection-note");
    var pageAll = document.getElementById("fleet-select-all-page");
    var bar = document.getElementById("fleet-selbar");
    var bulkForm = document.getElementById("bulk-command-form");
    var boxes = Array.prototype.slice.call(document.querySelectorAll("input.og-row-select"));

    function bulkReady() {
      if (!bulkForm) { return; }
      var n = selection.length;
      var setpoint = bulkForm.querySelector("[name=p_kw_setpoint]").value.trim();
      var reason = bulkForm.querySelector("[name=reason]").value.trim();
      var problems = [];
      if (n === 0) { problems.push("Select hubs in the table or on the map to enable this."); }
      if (proposeButton) { proposeButton.disabled = n === 0; }
      if (n && !setpoint) { problems.push("Enter a setpoint."); }
      if (n && !reason) { problems.push("A reason is required."); }
      if (selectionNote) {
        selectionNote.textContent = n === 0 ? problems[0]
          : "Ready to command " + plural(n, "hub") + "." + (problems.length ? " To propose: " + problems.join(" ") : "");
      }
    }

    function renderSelection() {
      og.map.markSelected(handle, selection);
      boxes.forEach(function (box) { box.checked = selection.indexOf(box.value) !== -1; });
      if (pageAll) {
        var onPage = boxes.filter(function (b) { return b.checked; }).length;
        pageAll.checked = boxes.length > 0 && onPage === boxes.length;
        pageAll.indeterminate = onPage > 0 && onPage < boxes.length;
      }
      if (idsField) { idsField.value = selection.join(","); }
      var n = selection.length;
      if (countEl) { countEl.textContent = n === 0 ? "No hubs selected" : plural(n, "hub") + " selected"; }
      if (bar) { bar.classList.toggle("is-active", n > 0); }
      bulkReady();
    }

    function setSelection(ids, additive) {
      if (additive) {
        ids.forEach(function (id) { if (selection.indexOf(id) === -1) { selection.push(id); } });
      } else {
        selection = ids.slice();
      }
      if (noteEl && !additive) { noteEl.textContent = ""; }
      renderSelection();
    }

    boxes.forEach(function (box) {
      box.addEventListener("click", function (event) { event.stopPropagation(); });
      box.addEventListener("change", function () {
        var at = selection.indexOf(box.value);
        if (box.checked && at === -1) { selection.push(box.value); }
        if (!box.checked && at !== -1) { selection.splice(at, 1); }
        renderSelection();
      });
    });
    if (pageAll) {
      pageAll.addEventListener("change", function () {
        var ids = boxes.map(function (b) { return b.value; });
        if (pageAll.checked) {
          setSelection(ids, true);
        } else {
          selection = selection.filter(function (id) { return ids.indexOf(id) === -1; });
          renderSelection();
        }
      });
    }
    var selPage = document.getElementById("fleet-select-page");
    if (selPage) {
      selPage.addEventListener("click", function () {
        setSelection(boxes.map(function (b) { return b.value; }), true);
      });
    }
    var selMatching = document.getElementById("fleet-select-matching");
    if (selMatching) {
      selMatching.addEventListener("click", function () {
        selMatching.disabled = true;
        fetch(basePath + "/fleet/selection" + (opts.query ? "?" + opts.query : ""), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (body) {
            selMatching.disabled = false;
            if (!body || !Array.isArray(body.hub_ids)) {
              if (noteEl) { noteEl.textContent = "Could not select: " + ((body && body.error) || "the API refused"); }
              return;
            }
            setSelection(body.hub_ids, false);
            if (noteEl) {
              noteEl.textContent = body.capped
                ? "Capped at " + body.max.toLocaleString() + ": more hubs match this filter."
                : "All " + plural(body.count, "matching hub") + ".";
            }
          })
          .catch(function () { selMatching.disabled = false; });
      });
    }

    function togglePick(hubId) {
      var at = selection.indexOf(hubId);
      if (at === -1) { selection.push(hubId); } else { selection.splice(at, 1); }
      renderSelection();
    }
    var areaSelect = og.map.enableAreaSelect(handle, setSelection, togglePick);
    var toggle = document.getElementById("fleet-select-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var on = toggle.getAttribute("aria-pressed") !== "true";
        toggle.setAttribute("aria-pressed", on ? "true" : "false");
        toggle.classList.toggle("is-active", on);
        areaSelect.set(on);
      });
    }
    ["fleet-select-clear", "fleet-selbar-clear"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener("click", function () { setSelection([], false); }); }
    });
    var goCommand = document.getElementById("fleet-selbar-command");
    if (goCommand) {
      goCommand.addEventListener("click", function (e) {
        e.preventDefault();
        var card = document.getElementById("bulk-card");
        card.scrollIntoView({ behavior: "smooth", block: "start" });
        var sp = document.getElementById("bulk-setpoint");
        if (sp) { sp.focus({ preventScroll: true }); }
      });
    }
    if (bulkForm) {
      bulkForm.addEventListener("input", bulkReady);
    }
    renderSelection();

    // ---- drawer ------------------------------------------------------------------------------
    var drawer = document.getElementById("hub-drawer");
    var lastRow = null;
    function openDrawer(row) {
      lastRow = row;
      document.querySelectorAll("tr.is-open").forEach(function (r) { r.classList.remove("is-open"); });
      row.classList.add("is-open");
      drawer.hidden = false;
      htmx.ajax("GET", basePath + "/fleet/hubs/" + encodeURIComponent(row.dataset.rowId), "#hub-drilldown-container");
      document.getElementById("hub-drawer-close").focus({ preventScroll: true });
    }
    function closeDrawer() {
      drawer.hidden = true;
      if (lastRow) { lastRow.classList.remove("is-open"); lastRow.focus(); }
    }
    document.getElementById("hub-drawer-close").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !drawer.hidden && !document.querySelector(".confirm-dialog:not([hidden])")) {
        closeDrawer();
      }
    });
    document.querySelectorAll("tr.clickable-row[data-row-id]").forEach(function (row) {
      row.addEventListener("click", function () { openDrawer(row); });
      row.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDrawer(row);
        }
      });
    });

    // ---- action cards -----------------------------------------------------------------------
    document.querySelectorAll("input[data-combobox]").forEach(function (input) { combobox(input, basePath); });
    document.querySelectorAll("select[data-scope-for]").forEach(scopeLink);
    document.querySelectorAll("form.fl-form").forEach(validation);
    var relSelect = document.getElementById("rel-request-id");
    if (relSelect) {
      relSelect.addEventListener("focus", function () {
        fetch(basePath + "/fleet/release-requests", { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (body) {
            if (!body || !Array.isArray(body.items)) { return; }
            var keep = relSelect.value;
            relSelect.innerHTML = "";
            body.items.forEach(function (r) {
              var o = document.createElement("option");
              o.value = r.proposal_id;
              o.textContent = r.proposal_id.slice(0, 8) + " · " + r.scope + ":" + r.scope_ref +
                " · by " + r.requested_by + " · " + Math.round(r.age_s) + " s ago";
              relSelect.appendChild(o);
            });
            if (!body.items.length) {
              var none = document.createElement("option");
              none.textContent = "No pending release requests";
              none.value = "";
              none.disabled = true;
              none.selected = true;
              relSelect.appendChild(none);
            } else if (keep) {
              relSelect.value = keep;
            }
            var btn = document.getElementById("release-review-button");
            var why = document.getElementById("release-review-why");
            btn.disabled = !body.items.length;
            why.textContent = body.items.length ? "" : "Nothing to approve: no release request is pending.";
          });
      });
    }
  }

  window.ogFleet = { init: init };
})();
