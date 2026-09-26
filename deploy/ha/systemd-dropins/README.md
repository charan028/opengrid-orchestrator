# systemd watchdog drop-ins (single-host HA, 2026-09-26)

Each `<unit>.watchdog.conf` here is installed as
`/etc/systemd/system/<unit>.service.d/watchdog.conf` (a drop-in override -- it does not replace or
duplicate the base unit file in `deploy/systemd/`, which stays LIVE-PATH's; this only *adds* the
`Type=notify` + `WatchdogSec=` lines).

```bash
for u in og-engine og-guardian og-safestop og-settle og-feeds og-api; do
  install -d "/etc/systemd/system/${u}.service.d"
  install -m 0644 "deploy/ha/systemd-dropins/${u}.watchdog.conf" "/etc/systemd/system/${u}.service.d/watchdog.conf"
done
systemctl daemon-reload
systemctl restart opengrid.target
```

Not applied by anything in this repo -- the owner or lead runs the above after the corresponding
process has been updated to call `opengrid.platform.watchdog.notify_ready()` once at startup and
`notify_watchdog()` once per main-loop iteration (see the platform build report's per-process wiring
lines). **Do not install a drop-in for a unit whose process doesn't call `notify_watchdog()` yet** --
`Type=notify` makes systemd wait for the initial `READY=1` before considering the unit started, and
`WatchdogSec=` kills a unit that never pings; either one applied prematurely takes the service down.

`WatchdogSec=20` here (ping every ~10s, `watchdog_interval_s()`'s default margin of 2) is a starting
point, not measured against real cycle timing -- tune per unit once each is wired and observed;
`og-engine`'s 2s dispatch cycle can ping every cycle, `og-feeds`' scheduler ticks less often and may
need a larger `WatchdogSec=`.
