# config/grid: map reference data for the operator console

Read by `opengrid.api.views_ext` (`GET /og/api/grid/layers`, `/og/api/fleet/map`, `/og/api/customers/map`).
Data only; nothing here is imported code.

| File | What it is | Source |
|---|---|---|
| `grid_map_data.json` | Weather-zone centroids and a one-time load snapshot, utility-scale storage sites, 7,089 transmission-line polylines (`path` is `[lon, lat]` pairs, GeoJSON order), the residential centroid, a 345 kV grid entry point and six corridor summaries | Copied read-only from the concept page on the base server, `/var/www/html/opengrid/grid_map_data.json` (2026-09-23 export, SHA-256 `eba8e48c85767a29bd5c046c181a2592d53aebecf4cb4e0efb93ece7c559b94d`). Not regenerated here. |
| `zone_centroids.json` | Display centroid and radius per load zone, used only while `og.hub.lat/lon` is NULL | Derived from `grid_map_data.json` (see its `_note`) |
| `customer_sites.json` | Placeholder customer-site positions | Hand-set placeholders, not real addresses |

## Provenance and licence notes for `grid_map_data.json`

The export's own note says it is real ERCOT, HIFLD and Tracking-the-Sun data, exported once.

- **Transmission lines:** Esri Living Atlas "US Electric Power Transmission Lines", which is derived from HIFLD
  (US federal open data). The HIFLD Open portal shut down on 2025-08-26, so the mirror is a continuity risk
  (`docs/orchestrator/02-architecture/04-external-data-integration.md` §11.7). Attribute Esri Living Atlas and
  HIFLD where the layer is shown.
- **Zone load snapshot:** ERCOT public data (NP6-345-CD), under ERCOT's terms of use. The API overlays the live
  `og.feed_obs` value on this snapshot.
- **Storage sites:** HIFLD / EIA-sourced public plant data (name, county, MW).
- **Residential centroid:** LBNL Tracking the Sun (public), kWh-weighted.

To refresh the file, re-run the export on the base server and copy it here again. Record the new SHA-256 above.
