---
version: alpha
name: OpenGrid Orchestrator UI
description: Calm modern console for grid operators. Layered near-black surfaces, one cool blue accent, tabular numerals, drawn icons, roomy operate-mode density. Dark by default; light is a mirrored token set.
colors:
  # Dark theme (default, `:root` / `[data-theme="dark"]`). Values are the literal tokens in static/og.css.
  primary: "#7cc4ff"
  accent: "#7cc4ff"
  kicker: "#7cc4ff"
  accent-ink: "#0a0c10"
  focus-ring: "#7cc4ff"
  bg: "#0a0c10"
  panel: "#12151b"
  panel-2: "#191d25"
  border: "#262b36"
  text: "#e8ebf1"
  muted: "#98a1b3"
  # Severity tokens: fixed by docs/orchestrator/04-ui/01-ui-ux-specification.md S5.
  status-good: "#1baf7a"
  status-info: "#7cc4ff"
  status-caution: "#eda100"
  status-critical: "#ee6160"
  status-neutral: "#98a1b3"
  # 7-series customer-type categorical chart palette: fixed order everywhere (UI-UX spec S5.2).
  series-ercot-energy: "#3987e5"
  series-ercot-as: "#199e70"
  series-partner-capacity: "#c98500"
  series-dist-deferral: "#1aa5b8"
  series-large-load: "#d55181"
  series-pipeline-ac: "#9085e9"
  series-pjm-capacity: "#008300"
  # Light theme (`[data-theme="light"]`): the same roles, mirrored.
  accent-light: "#1a5fc4"
  kicker-light: "#1a5fc4"
  accent-ink-light: "#ffffff"
  focus-ring-light: "#1a5fc4"
  bg-light: "#f4f6fa"
  panel-light: "#ffffff"
  panel-2-light: "#eef1f6"
  border-light: "#dde2ea"
  text-light: "#14171d"
  muted-light: "#5b6172"
  status-good-light: "#147a52"
  status-info-light: "#1861c7"
  status-caution-light: "#8f5f00"
  status-critical-light: "#ab2e2d"
  status-neutral-light: "#5b6172"
typography:
  body:
    fontFamily: "Geist, Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: '"cv11", "ss01", "tnum"'
  page-title:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 20px
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: -0.015em
  dialog-title:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 16px
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: -0.01em
  panel-title:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: -0.01em
  kpi-value:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 26px
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: -0.02em
    fontFeature: '"tnum"'
  small:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  control:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.5
  label:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.5
  column-header:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 11px
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: 0.05em
  badge:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 11px
    fontWeight: 600
    lineHeight: 18px
    letterSpacing: 0.03em
  chart-axis:
    fontFamily: "Geist, Inter, system-ui, sans-serif"
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.2
rounded:
  sm: 8px
  md: 12px
  lg: 16px
  full: 999px
spacing:
  2xs: 6px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 20px
  xl: 28px
  2xl: 32px
  page-x: 32px
  page-top: 28px
  page-bottom: 56px
  section-gap: 20px
  tile-gap: 12px
  nav-width: 236px
  content-max: 1600px
components:
  button:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.text}"
    typography: "{typography.control}"
    rounded: "{rounded.sm}"
    padding: 7px 14px
    height: 34px
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    typography: "{typography.control}"
    rounded: "{rounded.sm}"
    padding: 7px 14px
    height: 34px
  button-danger:
    backgroundColor: transparent
    textColor: "{colors.status-critical}"
    typography: "{typography.control}"
    rounded: "{rounded.sm}"
    padding: 7px 14px
    height: 34px
  button-safestop:
    backgroundColor: "color-mix(in srgb, #ee6160 12%, #12151b)"
    textColor: "{colors.status-critical}"
    typography: "{typography.control}"
    rounded: "{rounded.sm}"
    padding: 9px 18px
  input:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 7px 11px
    height: 36px
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: 20px 22px
  kpi-tile:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    typography: "{typography.kpi-value}"
    rounded: "{rounded.md}"
    padding: 16px 18px 14px
  status-badge:
    backgroundColor: "color-mix(in srgb, #1baf7a 12%, transparent)"
    textColor: "{colors.status-good}"
    typography: "{typography.badge}"
    rounded: "{rounded.full}"
    padding: 2px 8px 2px 7px
  nav-item:
    backgroundColor: transparent
    textColor: "{colors.muted}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 8px 10px
  nav-item-active:
    backgroundColor: "color-mix(in srgb, #7cc4ff 14%, transparent)"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 8px 10px
  confirm-dialog:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: 24px 26px 22px
    width: 460px
---

# Design System: OpenGrid Orchestrator UI

Source of truth: `static/og.css` (tokens, components), `static/og.js` (ECharts theme built from the same tokens), `templates/base.html` (shell, inline Lucide symbol sheet), `templates/_partials/*.html` (kpi_tile, status_badge, stale_badge, data_table, confirm_dialog). This file describes what shipped; when it disagrees with `og.css`, `og.css` wins and this file is stale.

## Overview

**Creative North Star: "The Calm Console"**

A control room that stays quiet until something needs attention. Surfaces are layered near-black (Linear/Vercel register), text is a soft off-white, and exactly one cool blue carries every "look here" signal: primary action, active nav, links, focus ring, first chart series. Colour otherwise belongs to severity and to the categorical chart palette, both of which are fixed by the UI-UX specification and are not design choices to revisit per screen.

Density is roomy Operate-mode: 14px body, 20px section gaps, panels padded 20/22px, tiles that breathe. Numbers are the content, so every numeric run is tabular. Icons are drawn Lucide outlines at one stroke weight. Motion is a single settle-in on first paint and 160ms state transitions; nothing else moves. Dark is the default and primary theme; `[data-theme="light"]` mirrors every token for daytime and print and is toggled per viewer.

**Key Characteristics:**
- One accent, many severities: blue means "interactive/primary"; green/amber/red/blue-info/grey mean state, never decoration.
- Status is colour + icon + text, never colour alone.
- Every value on screen shows its age (`.stale-badge`); staleness flips to caution.
- Tonal layering (bg -> panel -> panel-2) with a 1px border and a top-edge catch-light does the depth work; shadows are soft and ambient.
- All text tokens hold >= 4.5:1 on `--bg`, `--panel`, and `--panel-2` in both themes; `tests-e2e/ui/test_a11y.py` computes this from `og.css` and fails the build otherwise.

## Colors

Three near-black neutrals stacked under one cool blue, with a fixed severity set and a fixed seven-series chart palette. Values are normative in the frontmatter; the dark set is `:root`, the `-light` suffixed set is `[data-theme="light"]`.

### Primary
- **Console Blue** (`primary` = `accent`; #7cc4ff dark / #1a5fc4 light; `primary` exists for the DESIGN.md linter, `accent` is the CSS name): the only interactive hue. Primary button fill, active nav tint (14% mix), links, caret, selection (35% mix), focus ring, input focus border and 3px halo (25% mix), first chart series, the single-line area fill (28% -> 0 gradient), and the two body radial glows (`--glow` 10% dark / 7% light). `kicker` is an alias that must stay a literal hex equal to `accent`; the a11y test parser reads it by name.
- **Accent Ink** (`accent-ink`; #0a0c10 dark / #ffffff light): text on a solid accent fill only.

### Neutral
- **Ground** (`bg`; #0a0c10 / #f4f6fa): page canvas and the 82% translucent sticky header.
- **Panel** (`panel`; #12151b / #ffffff): cards, nav rail, dialog, kanban cards, table header, tooltip card.
- **Panel 2** (`panel-2`; #191d25 / #eef1f6): inputs, default buttons, nested KPI tiles, kanban columns, `pre`, hover tint for nav and table rows.
- **Border** (`border`; #262b36 / #dde2ea): every 1px hairline; table row dividers at 60% mix; chart grid lines at 60% alpha; scrollbar thumb.
- **Text** (`text`; #e8ebf1 / #14171d) and **Muted** (`muted`; #98a1b3 / #5b6172): muted carries labels, captions, table headers, nav at rest, axis labels, and KPI units.

### Severity (fixed: UI-UX spec S5)
- **Good** (#1baf7a / #147a52), **Info** (#7cc4ff / #1861c7), **Caution** (#eda100 / #8f5f00; the light value was darkened from #986600, which measured 4.38:1 on `--panel-2`), **Critical** (#ee6160 / #ab2e2d), **Neutral** (#98a1b3 / #5b6172). Used as text colour plus a 12% tinted background on badges, as the KPI value colour under `data-severity`, and as the dot/border/tint on banners and at-risk cards. `--status-critical` is #ee6160 rather than the spec-adjacent #e34948 because 13px danger/safe-stop text must clear 4.5:1 on `--panel-2` and on the safe-stop fill.

### Chart series (fixed order: UI-UX spec S5.2)
`series-ercot-energy`, `series-ercot-as`, `series-partner-capacity`, `series-dist-deferral`, `series-large-load`, `series-pipeline-ac`, `series-pjm-capacity`. The ECharts theme in `og.js` puts `accent` first, then these; screens pass `token:--name` or `token:--name@alpha` strings instead of hex so no Python view carries a colour.

### Named Rules
**The One Blue Rule.** If it is blue and not a status badge or the first chart series, it is interactive. Do not use `accent` for emphasis on static text.
**The Never-Colour-Alone Rule.** A state is shown as colour + dot + drawn icon + text (`status_badge.html`). A KPI's severity colour is accompanied by its label and its stale badge.
**The Same-Roles Rule.** Light theme changes values, never roles. Add a colour to both blocks or to neither.

## Typography

**Display Font:** Geist (with Inter, then system-ui)
**Body Font:** Geist (with Inter, then system-ui)
**Label/Mono Font:** none; numerals use `tnum` in the body face.

**Character:** one neutral grotesk at four weights (400/500/600/650-700), tight negative tracking on titles, tabular numerals everywhere numbers appear. Nothing on screen exceeds 26px; hierarchy comes from weight, colour (text vs muted), and case, not size jumps.

### Hierarchy
- **KPI value** (600, 26px, 1.1, -0.02em, tabular): the largest type in the system; one per tile, unit beside it in 12px muted.
- **Page title** (650, 20px, -0.015em; 18px under 900px): the `<h1>` in the sticky header, exactly one per screen.
- **Dialog title** (650, 16px, -0.01em): confirm dialog heading.
- **Panel title** (600, 15px, -0.01em): `.og-panel h2`, 14px bottom margin, `text-wrap: balance`.
- **Body** (400, 14px, 1.5): default; `font-feature-settings: "cv11", "ss01", "tnum"` on `html`.
- **Small** (400, 13px): table cells, panel intro paragraph, dialog copy, banners, `dl` details.
- **Control** (500, 13px): buttons; primary 600; safe-stop 700 uppercase 0.04em.
- **Label** (500, 12px, muted): form captions, KPI labels, nav role, theme toggle, kanban cards.
- **Column header** (600, 11px, uppercase, 0.05em, muted): table `th` and kanban column headings.
- **Badge** (600, 11px, uppercase, 0.03em, 18px line): status badges; stale badge is 500 non-uppercase.
- **Chart axis** (400, 11px, muted): ECharts axis/legend; tooltip 12px `text`.

### Named Rules
**The Tabular Rule.** Any element that can contain a number (KPI value, `td.num`, `dd`, stale badge, kanban count, countdown) sets `font-variant-numeric: tabular-nums` and right-aligns in tables.
**The Uppercase-Is-Structural Rule.** Uppercase is used for column headers, kanban column headings, status badges, and the safe-stop control: things that name a category or a hazard. No uppercase intro lines, eyebrows, or kickers above headings.

## Layout

Two-column shell: a 236px sticky nav rail (`panel`, 1px right border, 100vh) and a fluid main column. Main holds a sticky, blurred (12px) 82%-`bg` header padded 16/32px, then `.og-content` padded 28px 32px 56px, capped at 1600px, laid out as a vertical flex with 20px section gaps. Self-polling screens wrap sections in `[id$="-poll-wrapper"]`, which repeats that 20px rhythm.

Grids: `.og-grid` is `auto-fit, minmax(300px, 1fr)` at 20px gap; `.og-kpis` is `auto-fit, minmax(150px, 1fr)` at 12px gap; `.og-kanban` is five equal columns at 12px gap; `.og-form` is `auto-fit, minmax(170px, 1fr)` at 14px/16px gap with items aligned to the baseline end. Grid children get `min-width: 0; overflow: hidden` so a chart canvas cannot widen its column.

Spacing rhythm observed: 6, 8, 10, 12, 14, 16, 18, 20, 22, 28, 32, 56px. Inside components the step is 6-8px; between components 12px (tiles) or 20px (sections); page gutters 32px.

Responsive (desktop first, 1280px reference): at <= 1100px the kanban wraps to `minmax(190px, 1fr)`. At <= 900px the nav becomes a sticky top bar (row layout, brand text hidden, screen list horizontally scrollable with a right-edge fade mask, role hidden, theme toggle icon-only), the header goes static and unblurred at 14/16px padding, content padding drops to 16px with 14px gaps, panels pad 16px, `.og-grid` collapses to one column, `.og-kpis` to two, kanban to one, map to 300px, forms to two columns with the action spanning both, and toolbar labels stack.

**The Twenty Rule.** Sections are 20px apart and panels are padded 20px; anything tighter is inside a component, anything looser is a page margin.

## Elevation & Depth

Hybrid: tonal layering does the structure, shadows add only ambience. Depth order is `bg` -> `panel` -> `panel-2`, each step separated by a 1px `border`. Layered surfaces (panels, KPI tiles) also carry `inset 0 1px 0 var(--edge)`, a top-edge catch-light (white at 4.5% dark, 90% light). Two fixed radial accent glows behind the canvas (`body::before`, 10% dark / 7% light) give the near-black ground dimension without texture. The sticky header and the dialog backdrop use `backdrop-filter: blur` (12px and 8px) over a translucent `bg`.

### Shadow Vocabulary
- **Ambient** (`--shadow`: `0 1px 2px rgba(0,0,0,.4), 0 16px 40px -24px rgba(0,0,0,.8)` dark; `rgba(20,23,29,.05)/.25` light): panels, standalone KPI tiles, kanban cards.
- **Pop** (`--shadow-pop`: `0 4px 12px -4px rgba(0,0,0,.6), 0 32px 80px -32px rgba(0,0,0,.9)` dark; `.12/.35` light): the confirm dialog only.
- **Accent lift** (`0 6px 16px -10px color-mix(accent 90%, transparent)`): primary button; the nav mark uses the same at -8px/80%.
- **Chart tooltip** (`0 12px 32px -16px rgba(0,0,0,.6)`, 10px radius): set in `og.js`.

### Named Rules
**The Nested-Goes-Flat Rule.** A KPI tile inside a panel drops its shadow and border and sits on `panel-2`; only top-level surfaces cast shadows.
**The Soft-Only Rule.** Shadows are large-blur, negative-spread, black-alpha. No hard offsets, no coloured shadows other than the accent lift under the primary button and nav mark.

## Shapes

Gently rounded, never pill-shaped except for counters and badges. Panels and KPI tiles use 12px (`rounded.md`); buttons, inputs, nav items, kanban columns and cards, `pre`, banners, empty states, and the map use 8px (`rounded.sm`); the confirm dialog is 16px (`rounded.lg`); status badges, the header stale badge, kanban counts, scrollbar thumb, and the empty-table dash are 999px (`rounded.full`); status dots are circles (6-8px). Hairlines are 1px `border`; the safe-stop button is the only 2px border. Empty states use a 1px dashed `border`. The nav brand mark is a 28px square at 8px radius filled with `accent`.

## Components

### Buttons
- **Shape:** 8px radius, 34px min height, 13px/500, 7px 14px padding, 8px icon gap, `white-space: nowrap`.
- **Default (`.btn`):** `panel-2` fill, 1px `border`, `text`. Hover lifts the border toward `muted` (45% mix) and the fill 4% toward `text`. Active scales to 0.98. Disabled is 50% opacity with `not-allowed`.
- **Primary (`.btn-primary`):** solid `accent`, `accent-ink` text, 600 weight, accent lift shadow; hover mixes 12% white in.
- **Danger (`.btn-danger`):** transparent, `status-critical` text 600, border critical at 60%; hover 10% critical tint, solid critical border.
- **Safe-stop (`.btn-safestop`):** must stay visually distinct and hard to hit by accident: 2px solid critical border, 12% critical over `panel` fill, critical text 700 uppercase 0.04em, 9px 18px padding, `nowrap`, spans two form columns. Hover deepens the tint to 18%. Never restyle it toward the other variants.
- **Transitions:** background, border-color, transform, box-shadow at 160ms `--ease`.

### Inputs / Fields
- **Style:** `panel-2` fill, 1px `border`, 8px radius, 7px 11px padding, 36px min height, inherits body type; placeholder is `muted` at 80%.
- **Hover:** border toward `muted` (45% mix).
- **Focus:** outline removed; border `accent` plus a 3px 25%-accent halo, 160ms `--ease`.
- **Labels:** a `<label>` wrapping its control stacks caption (12px/500 muted) above field with 6px gap; `.og-toolbar label` flips to a single row with 8px gap and 34px controls. Every control has a label or `aria-label` (tested).

### Cards / Containers
- **Panel (`.og-panel`):** 12px radius, `panel` fill, 1px `border`, 20px 22px padding (16px under 900px), ambient shadow plus edge catch-light. Title is 15px/600; the first paragraph is the 13px muted intro pulled up 8px; a `dl` renders as a two-column `max-content 1fr` grid at 8px 20px gap with tabular `dd`.
- **Empty state (`.og-empty`, `td.empty`):** centered 13px muted on a 1px dashed border, 8px radius; the table variant draws a 28x2px `border`-coloured dash above the text.
- **Degraded banner:** 14% caution over `panel`, 45% caution border, 8px radius, an 8px caution dot, 13px text, `role="alert"`.

### KPI Tile (`kpi_tile.html`)
Label (12px/500 muted), value (26px/600 tabular, unit 12px muted at baseline, 6px gap), then a stale badge pushed to the bottom. `data-severity="good|caution|critical"` colours the value only. Standalone tiles match panels (12px radius, border, shadow); nested in a panel they go flat on `panel-2` with 12px 14px 10px padding. `tile_id` and `live_region` let SSE handlers update the value in place.

### Status Badge (`status_badge.html`)
Pill (999px), 11px/600 uppercase 0.03em, 2px 8px 2px 7px padding, 5px gaps: a 6px dot, a 12px drawn icon at 2.5 stroke (check / crosshair / triangle / octagon / dash), then text. Text colour is the severity token; background is that token at 12%. Class families map many statuses to five severities (e.g. `online|good|ok|closed|acked|pass` -> good; `stale|warning|degraded|timeout|expired` -> caution; `offline|fault|breach|fail|engaged` -> critical).

### Stale Badge (`stale_badge.html`)
Inline 11px/500 muted tabular text ("age: 3s") after a 6px good dot. `data-stale="true"` turns text and dot caution, weight 600, and appends "· stale". `og.js` re-renders every `[data-since]` each second and flips the flag past `data-stale-after`; future-stamped values read "ahead: 2h". In the header actions it gains a `panel` pill with a 1px border and 4px 10px padding.

### Data Table (`data_table.html`)
Wrapped in a horizontally scrolling `.og-table-wrap` (-6px/6px bleed). 13px body, collapsed borders. Sticky `panel` header row: 11px/600 uppercase 0.05em muted, 6px 12px 10px padding, 1px `border` underline. Cells pad 10px 12px with a 60% `border` divider (none on the last row); hover tints the row 70% `panel-2`. Numeric columns (`.num`) right-align with tabular numerals and carry units in the header. `clickable-row` rows are focusable with an inset 2px focus ring at 8px radius.

### Confirm Dialog (`confirm_dialog.html`)
Two-step confirmation (propose, then confirm). Fixed backdrop of 70% `bg` with 8px blur at z-index 100; the dialog is `panel`, 1px `border`, 16px radius, 24px 26px 22px padding, max 460px, pop shadow, and settles in over 160ms. `role="alertdialog"`, `aria-modal`, labelled by its 16px/650 title; 13px muted summary; an optional tabular countdown that disables Confirm at zero. Actions right-aligned with 8px gap, 20px above: a default Cancel and a danger or safe-stop Confirm. Focus moves to Cancel on open and returns to the trigger on close; Tab cycles between the two buttons; Escape closes.

### Navigation
Rail items are 13-14px/500 muted with an 18px icon at 85% opacity, 8px 10px padding, 8px radius, 2px apart. Hover: `panel-2` fill, `text`. Active (`aria-current="page"`): 14% accent tint, `text` at 600, icon at full opacity in `accent`. The brand row is 15px/650 with an 11px/500 muted "Orchestrator" line and the 28px accent mark (zap glyph, filled, 2.25 stroke). Footer: role line (12px muted; shield icon in good for operator, eye for viewer) and a bordered theme toggle (12px/500, 120ms ease-out hover). Under 900px the rail becomes the top bar described in Layout.

### Kanban (`.og-kanban`, Dispatch)
Five `panel-2` columns (8px radius, 10px padding, 120px min height, 8px gaps) headed by an 11px uppercase muted title with a `panel` pill count. Cards are `panel`, 1px border, 8px radius, 10px 12px padding, 12px text with a 13px/600 tabular first line, ambient shadow; hover lifts the border. `.og-at-risk` cards take a 60% caution border and 6% caution tint.

### Icons
One inline SVG symbol sheet in `base.html` (Lucide outlines: zap, dashboard, battery, branch, trend, pulse, dollar, scroll, sun, moon, shield, eye, check, alert, octagon, minus), referenced with `<use href="#i-...">`. `.og-icon` is 18px, `stroke: currentColor`, `fill: none`, 1.5 stroke, round caps and joins. Exceptions in the build: badge icons are 12px at 2.5 stroke (inlined, not `<use>`d, so HTMX fragments carry their own geometry); the brand mark is a filled 16px zap at 2.25. Icons are always `aria-hidden` next to text.

### Charts (`og.js`)
`og.chart(el, option)` applies one registered ECharts theme per data-theme: transparent background, series order `accent` then the seven series tokens, hidden axis lines and ticks, dashed `[4,6]` 60%-`border` split lines on value axes only, 11px muted axis and legend text (10px circle legend markers), a `panel` tooltip card with 1px `border`, 8/12px padding, 10px radius. Lines are 2px, `smooth: 0.35`, no symbols; a lone line series gets an accent area gradient (28% -> 0). Bars cap at 56px with 4px top radius. ISO timestamps display as HH:MM with overlap hiding. Empty data hides axes and legend and centers "No data yet" in 13px muted. Canvas animation is disabled under `prefers-reduced-motion`.

### Map (`.og-map`)
420px (300px mobile), 8px radius, 1px border, `panel-2` ground. Dark theme inverts and desaturates the Leaflet tile pane (`invert(1) hue-rotate(180deg) saturate(.55) brightness(.9) contrast(.95)`); zoom controls and attribution are re-skinned to `panel`/`text`/`border`.

### Motion
- **Settle-in:** `.og-content > *` runs `og-settle` (opacity 0 -> 1, translateY 6px -> 0) over 180ms with `cubic-bezier(0.16, 1, 0.3, 1)`, `fill-mode: backwards`, staggered 30ms per child up to 150ms. The dialog reuses it at 160ms with `both`. This is the one authored moment (spec S5.7: <= 200ms).
- **State transitions:** 160ms on `--ease` (`cubic-bezier(0.32, 0.72, 0, 1)`) for nav, buttons, and inputs. The theme toggle is the one outlier at 120ms ease-out.
- **Reduced motion:** all animation and transition durations collapse to 0.01ms; `og.js` disables chart animation.
- **Focus:** every interactive element shows `outline: 2px solid var(--focus-ring)` at 2px offset (inset for table rows); the light theme redefines `--focus-ring` so the ring holds >= 3:1 on light surfaces.

## Do's and Don'ts

### Do:
- **Do** take every colour from a token: `var(--x)` in CSS, `token:--x` or `token:--x@0.2` in chart options. No hex in templates, Python, or `og.js` beyond the theme registration.
- **Do** add a new text token to both theme blocks and keep it >= 4.5:1 on `--bg`, `--panel`, and `--panel-2` in each; `tests-e2e/ui/test_a11y.py` will fail otherwise.
- **Do** keep `--kicker` a literal hex identical to `--accent` in both themes; the contrast parser reads it by name and does not resolve `var()`.
- **Do** keep the severity tokens and the seven series tokens at their spec values and order (UI-UX spec S5); build new state colours by mixing them (`color-mix(in srgb, var(--status-x) 12%, transparent)`), not by inventing hues.
- **Do** render state as `status_badge.html` (dot + icon + text) and attach `stale_badge.html` to any displayed value.
- **Do** use the existing partials for tiles, tables, badges, and confirmations before writing new markup; any operator action that changes the grid goes through the two-step confirm dialog.
- **Do** put new sections directly under `.og-content` (or the poll wrapper) so they inherit the 20px rhythm and the settle-in.

### Don't:
- **Don't** restyle `.btn-safestop` toward the primary or danger variants; it stays 2px critical border, tinted fill, uppercase 700, and larger padding.
- **Don't** use `accent` on static text, headings, or decoration; blue means interactive, info-severity, or first series.
- **Don't** add uppercase intro lines or eyebrows above headings; uppercase is reserved for column headers, kanban headings, badges, and safe-stop.
- **Don't** add motion beyond the settle-in and 160ms state transitions, and never bypass `prefers-reduced-motion`.
- **Don't** add hard-offset or coloured shadows, icon fonts, glyph icons, or a second icon stroke weight; extend the `base.html` symbol sheet with Lucide outlines at 1.5.
- **Don't** let a grid child set its own min-width or overflow; chart canvases have already widened columns once.
