/* opengrid.ui shared grid map (CR #19 items 1 and 2). Owner: ui-a.
 *
 * One Leaflet module behind both map surfaces -- the Control room's situational map and the Fleet
 * screen's selection map -- so the two never drift in colour, legend wording or interaction. Built to
 * match the simulators' reference map (`/opengrid/service.php?type=grid_arbitrage`): real transmission
 * backbone coloured by voltage class, real ERCOT zone load, real utility-scale batteries, the fleet's
 * grid connection, and the homes themselves coloured by what they are doing right now.
 *
 * Interaction is deliberately the browser default set the reference has: wheel zoom, drag pan, pinch on
 * touch, the +/- buttons, plus per-layer toggles and a legend. Nothing here auto-plays (UI-UX spec S5.7:
 * a control-room map must never animate itself while an operator is reading it).
 */
(function () {
  "use strict";

  const og = window.og || (window.og = {});
  const map = {};

  // ---- vocabulary ------------------------------------------------------------------------------

  /** Hub activity (CR #19's `activity` enum). Colour + label + shape: never colour alone. */
  map.ACTIVITY = [
    { key: "DELIVERING", label: "Delivering to a customer", token: "--status-good" },
    { key: "HOME_USE", label: "Serving its own home", token: "--series-dist-deferral" },
    { key: "CHARGING", label: "Charging", token: "--series-ercot-energy" },
    { key: "IDLE", label: "Idle (available)", token: "--status-neutral" },
    { key: "FAULT", label: "Fault", token: "--status-critical" },
    { key: "OFFLINE", label: "Offline or stale", token: "--status-neutral", hollow: true },
  ];

  /** Hub health (health/rules.py classify_hub_health), worst first. */
  map.HEALTH = [
    { key: "fault", label: "Fault", token: "--status-critical" },
    { key: "offline", label: "Offline", token: "--status-neutral", hollow: true },
    { key: "stale", label: "Stale", token: "--status-caution" },
    { key: "online", label: "Online", token: "--status-good" },
  ];

  /** ERCOT load-zone centroids: where a zone's hubs are drawn until they carry their own coordinates. */
  map.ZONE_CENTROIDS = {
    LZ_NORTH: [32.85, -97.0],
    LZ_WEST: [31.9, -102.3],
    LZ_HOUSTON: [29.76, -95.37],
    LZ_SOUTH: [29.4, -98.5],
  };

  // Transmission colour by voltage class, darker = higher kV (the reference map's scale).
  const VOLTAGE_STEPS = [
    [500, "#104281"],
    [345, "#256abf"],
    [230, "#5598e7"],
    [0, "#9ec5f4"],
  ];

  function voltageColor(kv) {
    for (const [threshold, color] of VOLTAGE_STEPS) {
      if (kv >= threshold) {
        return color;
      }
    }
    return VOLTAGE_STEPS[VOLTAGE_STEPS.length - 1][1];
  }

  function activityMeta(key) {
    return map.ACTIVITY.filter(function (a) { return a.key === key; })[0] || map.ACTIVITY[3];
  }

  function healthMeta(key) {
    return map.HEALTH.filter(function (h) { return h.key === key; })[0] || map.HEALTH[1];
  }

  /**
   * The activity the API will send, derived from what `/og/api/fleet/hubs` already carries, so the map
   * is honest today and needs no change when `GET /og/api/fleet/map` starts serving `activity`:
   * a faulted or unreachable hub reports that first, otherwise the sign of its power tells us whether it
   * is discharging (delivering) or charging. `HOME_USE` needs the obligation behind the discharge, which
   * only the map endpoint knows; until then a serving hub reads DELIVERING.
   */
  map.activityOf = function activityOf(hub) {
    if (hub.activity) {
      return hub.activity;
    }
    const health = String(hub.health || "").toLowerCase();
    if (health === "fault") {
      return "FAULT";
    }
    if (health === "offline" || health === "stale") {
      return "OFFLINE";
    }
    const kw = Number(hub.kw != null ? hub.kw : hub.p_kw);
    if (!isNaN(kw) && kw > 0.1) {
      return "DELIVERING";
    }
    if (!isNaN(kw) && kw < -0.1) {
      return "CHARGING";
    }
    return "IDLE";
  };

  /**
   * A stable hash of a string in [0, 1): the same hub lands in the same place on every reload. FNV-1a
   * plus murmur3's finalizer -- the avalanche step matters here, because hub ids are sequential
   * (`hub-00000`, `hub-00001`, ...) and the raw FNV values of neighbours differ by so little that the
   * scattered points collapsed onto a bowtie instead of filling the zone (seen live).
   */
  function hash(text) {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    h ^= h >>> 15;
    h = Math.imul(h, 2246822507);
    h ^= h >>> 13;
    h = Math.imul(h, 3266489909);
    h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  }

  /**
   * Where to draw a hub. Real `lat`/`lon` win. Otherwise the hub is scattered deterministically inside
   * its load zone (radius ~55 km around the zone centroid), exactly the way the reference map scatters
   * homes inside a ZIP: the geography is indicative, the zone is real. Callers must label it -- see the
   * "positions derived" note in the Control room legend.
   */
  map.hubLatLng = function hubLatLng(hub) {
    if (hub.lat != null && hub.lon != null) {
      return [Number(hub.lat), Number(hub.lon)];
    }
    const centre = map.ZONE_CENTROIDS[hub.zone];
    if (!centre) {
      return null;
    }
    const id = String(hub.hub_id || "");
    const angle = hash(id) * 2 * Math.PI;
    const radiusKm = 55 * Math.sqrt(hash(id + ":r"));
    const dLat = (radiusKm / 111) * Math.cos(angle);
    const dLon = (radiusKm / (111 * Math.cos((centre[0] * Math.PI) / 180))) * Math.sin(angle);
    return [centre[0] + dLat, centre[1] + dLon];
  };

  map.derivedPositions = function derivedPositions(hubs) {
    return (hubs || []).every(function (h) { return h.lat == null || h.lon == null; });
  };

  // ---- the map ---------------------------------------------------------------------------------

  /**
   * Create the map on `el`. Returns a handle: `{ map, layers, addLayer, ... }`. `layers` holds every
   * named overlay so the toggle control and the callers can reach them.
   */
  map.create = function create(el, opts) {
    opts = opts || {};
    const node = typeof el === "string" ? document.querySelector(el) : el;
    if (!node) {
      throw new Error("og.map.create: element not found: " + el);
    }
    const reduced = og.reducedMotion();
    const leaflet = L.map(node, {
      zoomControl: true,
      scrollWheelZoom: true, // wheel zoom, drag pan and pinch: the reference map's interaction set
      dragging: true,
      touchZoom: true,
      doubleClickZoom: true,
      zoomAnimation: !reduced,
      fadeAnimation: !reduced,
      markerZoomAnimation: !reduced,
      preferCanvas: true,
    }).setView(opts.center || [31.0, -99.0], opts.zoom || 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap",
      maxZoom: 18,
    }).addTo(leaflet);
    // Our own homes ride in their own pane above the reference layers: the zone-load and utility-battery
    // bubbles are far larger, and whichever order the callers add them in, the fleet must stay on top.
    leaflet.createPane("ogHubs").style.zIndex = 450;
    return {
      map: leaflet,
      layers: {},
      canvas: L.canvas({ padding: 0.5, pane: "ogHubs" }),
      addLayer: function (name, layer, visible) {
        this.layers[name] = layer;
        if (visible !== false) {
          layer.addTo(leaflet);
        }
        return layer;
      },
    };
  };

  /** Fetch the static reference layers (real ERCOT/HIFLD export shipped next to this file). */
  map.loadGrid = function loadGrid(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) {
        throw new Error("grid layers " + r.status);
      }
      return r.json();
    });
  };

  /**
   * The reference layers: transmission backbone by kV, ERCOT zone load, utility-scale batteries and the
   * fleet's grid connection point. Each is its own toggleable overlay.
   */
  map.addGridLayers = function addGridLayers(handle, grid) {
    const transmission = L.layerGroup();
    (grid.transmission || []).forEach(function (seg) {
      // paths are [lon, lat] pairs in the source export
      const latlngs = seg.path.map(function (p) { return [p[1], p[0]]; });
      L.polyline(latlngs, {
        color: voltageColor(seg.kv),
        weight: seg.kv >= 500 ? 2 : 1.3,
        opacity: 0.75,
        interactive: true,
      })
        .bindPopup("<strong>" + seg.kv + " kV line</strong><br>Transmission backbone (real, HIFLD)")
        .addTo(transmission);
    });
    handle.addLayer("transmission", transmission);

    const maxLoad = Math.max.apply(
      null,
      (grid.zones || []).map(function (z) { return z.load_mw || 0; }).concat([1])
    );
    const zoneLoad = L.layerGroup();
    (grid.zones || []).forEach(function (z) {
      if (z.load_mw == null) {
        return;
      }
      L.circleMarker([z.lat, z.lon], {
        radius: 8 + 18 * Math.sqrt(z.load_mw / maxLoad),
        color: og.token("--series-ercot-energy"),
        fillColor: og.token("--series-ercot-energy"),
        fillOpacity: 0.35,
        weight: 1.5,
      })
        .bindPopup(
          "<strong>" + z.name + " zone</strong><br>Load " +
          Math.round(z.load_mw).toLocaleString() + " MW (real ERCOT)" +
          (z.nearest_storage ? "<br>Nearest storage: " + z.nearest_storage : "")
        )
        .addTo(zoneLoad);
    });
    handle.addLayer("zoneLoad", zoneLoad);

    const batteries = L.layerGroup();
    (grid.utility_batteries || []).forEach(function (s) {
      L.circleMarker([s.lat, s.lon], {
        radius: 4 + Math.min(18, s.mw / 15),
        color: og.token("--series-pipeline-ac"),
        fillColor: og.token("--series-pipeline-ac"),
        fillOpacity: 0.55,
        weight: 1,
      })
        .bindPopup(
          "<strong>" + s.name + "</strong><br>" + (s.county ? s.county + " County<br>" : "") +
          "Utility battery " + Math.round(s.mw) + " MW (real, HIFLD)"
        )
        .addTo(batteries);
    });
    handle.addLayer("utilityBatteries", batteries);

    const entry = grid.grid_entry_point;
    if (entry) {
      const connection = L.layerGroup();
      L.circleMarker([entry.lat, entry.lon], {
        radius: 7,
        color: og.token("--text"),
        fillColor: og.token("--bg"),
        fillOpacity: 0.9,
        weight: 3,
      })
        .bindPopup(
          "<strong>Fleet grid connection</strong><br>" + (entry.voltage_kv || "?") +
          " kV, " + (entry.distance_km != null ? entry.distance_km.toFixed(1) + " km away" : "") +
          "<br>Where this fleet reaches the transmission network (real)"
        )
        .addTo(connection);
      handle.addLayer("gridConnection", connection);
    }
    return handle;
  };

  /**
   * Best sell destination this hour. The real per-zone settlement price arrives with
   * `GET /og/api/grid/layers`; until then the highest-load zone is the scarcity proxy, and the popup and
   * legend say exactly that rather than implying a price we do not have.
   */
  map.addBestDestination = function addBestDestination(handle, grid) {
    const zones = (grid.zones || []).filter(function (z) { return z.load_mw != null; });
    if (!zones.length || !grid.grid_entry_point) {
      return handle;
    }
    const best = zones.slice().sort(function (a, b) { return b.load_mw - a.load_mw; })[0];
    const entry = grid.grid_entry_point;
    const group = L.layerGroup();
    L.polyline([[entry.lat, entry.lon], [best.lat, best.lon]], {
      color: og.token("--status-caution"),
      weight: 3,
      opacity: 0.9,
    }).addTo(group);
    L.circleMarker([best.lat, best.lon], {
      radius: 10,
      color: og.token("--status-caution"),
      fillColor: og.token("--status-caution"),
      fillOpacity: 0.5,
      weight: 2,
    })
      .bindPopup(
        "<strong>Best sell destination this hour</strong><br>" + best.name + " &mdash; " +
        Math.round(best.load_mw).toLocaleString() + " MW of load, the highest in ERCOT right now." +
        "<br><em>Proxy: highest zone load. The settlement-point price replaces it when " +
        "/og/api/grid/layers lands.</em>"
      )
      .addTo(group);
    handle.addLayer("bestDestination", group);
    return handle;
  };

  /**
   * Demand heat: how much of each zone's load this fleet could actually serve right now. `served` is the
   * fleet's available kW in that zone, `unserved` the rest of the zone's demand -- the CR's
   * "high-demand areas, served vs not served", as two rings rather than a smeared heat blur so the split
   * stays readable and colour is never the only cue (the popup states both numbers).
   */
  map.addDemandLayer = function addDemandLayer(handle, cells) {
    const group = L.layerGroup();
    (cells || []).forEach(function (cell) {
      const served = Number(cell.served_kw || 0);
      const unserved = Number(cell.unserved_kw || 0);
      const total = served + unserved;
      if (!total || cell.lat == null) {
        return;
      }
      const outer = 16 + 26 * Math.sqrt(Math.min(1, total / (cell.scale_kw || total)));
      // Red where this fleet has nothing in the zone, amber where it is present but short of the
      // demand: the CR's "served vs not served", with the exact kW in the popup either way.
      const outerToken = served > 0 ? "--status-caution" : "--status-critical";
      L.circleMarker([cell.lat, cell.lon], {
        radius: outer,
        color: og.token(outerToken),
        fillColor: og.token(outerToken),
        fillOpacity: 0.14,
        weight: 1,
        dashArray: "4,4",
      })
        .bindPopup(
          "<strong>" + (cell.name || "Demand") + "</strong><br>" +
          "Demand " + Math.round(total).toLocaleString() + " kW<br>" +
          "This fleet can serve " + Math.round(served).toLocaleString() + " kW" +
          (cell.hubs != null ? " from " + cell.hubs + " hubs here" : "") + "<br>" +
          "Not served by this fleet " + Math.round(unserved).toLocaleString() + " kW"
        )
        .addTo(group);
      if (served > 0) {
        L.circleMarker([cell.lat, cell.lon], {
          radius: outer * Math.sqrt(served / total),
          color: og.token("--status-good"),
          fillColor: og.token("--status-good"),
          fillOpacity: 0.3,
          weight: 1,
        }).addTo(group);
      }
    });
    handle.addLayer("demand", group, false);
    return handle;
  };

  /** Our customer sites: consuming or not, with the live kW. */
  map.addCustomerLayer = function addCustomerLayer(handle, sites) {
    const group = L.layerGroup();
    (sites || []).forEach(function (site) {
      if (site.lat == null || site.lon == null) {
        return;
      }
      const consuming = !!site.consuming;
      L.marker([site.lat, site.lon], {
        icon: L.divIcon({
          className: "og-map-site" + (consuming ? " is-consuming" : ""),
          html: '<span aria-hidden="true"></span>',
          iconSize: [16, 16],
        }),
      })
        .bindPopup(
          "<strong>" + (site.name || site.customer_id || "Customer") + "</strong><br>" +
          (site.service_type || "customer") + "<br>" +
          (consuming ? "Consuming " + Number(site.kw || 0).toFixed(1) + " kW now" : "Not consuming")
        )
        .addTo(group);
    });
    handle.addLayer("customers", group);
    return handle;
  };

  /**
   * The homes. Canvas-rendered (thousands of individually styled markers is exactly the case Leaflet's
   * canvas renderer exists for). `opts.colorBy` is "activity" (Control room) or "health" (Fleet).
   */
  map.addHubLayer = function addHubLayer(handle, hubs, opts) {
    opts = opts || {};
    const byId = {};
    const group = L.layerGroup();
    (hubs || []).forEach(function (hub) {
      const at = map.hubLatLng(hub);
      if (!at) {
        return;
      }
      const activity = map.activityOf(hub);
      const meta = opts.colorBy === "health" ? healthMeta(hub.health) : activityMeta(activity);
      const color = og.token(meta.token);
      const marker = L.circleMarker(at, {
        renderer: handle.canvas,
        pane: "ogHubs",
        radius: 5,
        color: color,
        fillColor: meta.hollow ? "transparent" : color,
        fillOpacity: meta.hollow ? 0 : 0.75,
        weight: meta.hollow ? 1.5 : 1,
      });
      marker.ogHub = hub;
      marker.ogBaseColor = color;
      const serving = (hub.serving_obligations || []).length;
      const canServe = (hub.can_serve_services || []).join(", ");
      marker.bindPopup(
        "<strong>" + hub.hub_id + "</strong> &middot; " + (hub.bank_id || "") + "<br>" +
        (opts.colorBy === "health" ? healthMeta(hub.health).label : activityMeta(activity).label) +
        "<br>" +
        (hub.soc_pct != null
          ? "Charge " + hub.soc_pct + "%"
          : "SoC " + (hub.soc_kwh != null ? hub.soc_kwh + " kWh" : "n/a")) +
        "<br>Power " + Number(hub.kw != null ? hub.kw : hub.p_kw || 0).toFixed(2) + " kW" +
        (serving ? "<br>Serving " + serving + " obligation" + (serving === 1 ? "" : "s") : "") +
        (canServe ? "<br>Can serve: " + canServe : "")
      );
      byId[hub.hub_id] = marker;
      marker.addTo(group);
    });
    handle.addLayer("hubs", group);
    handle.hubMarkers = byId;
    return handle;
  };

  // ---- controls --------------------------------------------------------------------------------

  /** Layer toggles as real checkboxes, so the control is operable and announced like any other form. */
  map.addLayerToggles = function addLayerToggles(handle, items, idPrefix) {
    const control = L.control({ position: "topright" });
    control.onAdd = function () {
      const box = L.DomUtil.create("div", "og-map-layers");
      const title = document.createElement("p");
      title.className = "og-map-layers-title";
      title.textContent = "Layers";
      box.appendChild(title);
      items.forEach(function (item, index) {
        const layer = handle.layers[item.key];
        if (!layer) {
          return;
        }
        const row = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.id = (idPrefix || "og-layer") + "-" + item.key + "-" + index;
        input.checked = handle.map.hasLayer(layer);
        input.setAttribute("aria-label", "Show " + item.label + " layer");
        input.addEventListener("change", function () {
          if (input.checked) {
            handle.map.addLayer(layer);
          } else {
            handle.map.removeLayer(layer);
          }
        });
        row.appendChild(input);
        row.appendChild(document.createTextNode(item.label));
        box.appendChild(row);
      });
      L.DomEvent.disableClickPropagation(box);
      L.DomEvent.disableScrollPropagation(box);
      return box;
    };
    control.addTo(handle.map);
    return handle;
  };

  /** Legend: a swatch and its meaning in words, matching the reference map's caption line. */
  map.addLegend = function addLegend(handle, items, note) {
    const control = L.control({ position: "bottomleft" });
    control.onAdd = function () {
      const box = L.DomUtil.create("div", "og-map-legend");
      items.forEach(function (item) {
        const row = document.createElement("span");
        const dot = document.createElement("i");
        dot.className = "og-legend-" + (item.shape || "dot");
        dot.style.background = item.color;
        if (item.hollow) {
          dot.style.background = "transparent";
          dot.style.boxShadow = "inset 0 0 0 2px " + item.color;
        }
        row.appendChild(dot);
        row.appendChild(document.createTextNode(item.label));
        box.appendChild(row);
      });
      if (note) {
        const foot = document.createElement("span");
        foot.className = "og-map-legend-note";
        foot.textContent = note;
        box.appendChild(foot);
      }
      L.DomEvent.disableClickPropagation(box);
      return box;
    };
    control.addTo(handle.map);
    return handle;
  };

  /** Legend rows for the hub colouring in use. */
  map.hubLegendItems = function hubLegendItems(colorBy) {
    const source = colorBy === "health" ? map.HEALTH : map.ACTIVITY;
    return source.map(function (m) {
      return { label: m.label, color: og.token(m.token), hollow: !!m.hollow };
    });
  };

  // ---- area selection (Fleet) -------------------------------------------------------------------

  /**
   * Drag a rectangle over the map to select the hubs inside it (CR #19 item 2). Off by default: the map
   * pans normally until the operator turns selection on, so the usual gesture is never hijacked.
   * `onChange(idsInRectangle, additive)` is called once per completed drag.
   */
  map.enableAreaSelect = function enableAreaSelect(handle, onChange) {
    const leaflet = handle.map;
    const pane = leaflet.getContainer();
    let active = false;
    let origin = null;
    let box = null;

    function pointFor(event) {
      return leaflet.mouseEventToContainerPoint(event);
    }

    function draw(to) {
      if (!box) {
        box = L.DomUtil.create("div", "og-map-selectbox", pane);
      }
      box.style.left = Math.min(origin.x, to.x) + "px";
      box.style.top = Math.min(origin.y, to.y) + "px";
      box.style.width = Math.abs(origin.x - to.x) + "px";
      box.style.height = Math.abs(origin.y - to.y) + "px";
    }

    function cleanup() {
      if (box) {
        L.DomUtil.remove(box);
        box = null;
      }
      origin = null;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }

    function onMove(event) {
      if (origin) {
        draw(pointFor(event));
      }
    }

    function onUp(event) {
      if (!origin) {
        return;
      }
      const bounds = L.latLngBounds(
        leaflet.containerPointToLatLng(origin),
        leaflet.containerPointToLatLng(pointFor(event))
      );
      const ids = [];
      Object.keys(handle.hubMarkers || {}).forEach(function (id) {
        if (bounds.contains(handle.hubMarkers[id].getLatLng())) {
          ids.push(id);
        }
      });
      const additive = event.shiftKey;
      cleanup();
      onChange(ids, additive);
    }

    function onDown(event) {
      if (!active || event.button !== 0) {
        return;
      }
      L.DomEvent.preventDefault(event);
      origin = pointFor(event);
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }

    pane.addEventListener("mousedown", onDown);

    return {
      set: function (on) {
        active = !!on;
        leaflet.dragging[active ? "disable" : "enable"]();
        pane.classList.toggle("is-selecting", active);
      },
      isActive: function () { return active; },
    };
  };

  /** Paint the selected hubs so the map and the table agree at a glance. */
  map.markSelected = function markSelected(handle, ids) {
    const selected = {};
    (ids || []).forEach(function (id) { selected[id] = true; });
    Object.keys(handle.hubMarkers || {}).forEach(function (id) {
      const marker = handle.hubMarkers[id];
      const on = !!selected[id];
      marker.setStyle({
        color: on ? og.token("--accent") : marker.ogBaseColor,
        weight: on ? 3 : 1,
        radius: on ? 7 : 5,
      });
      if (on) {
        marker.bringToFront();
      }
    });
  };

  og.map = map;
})();
