# ogsim.common

Shared building blocks for `ogsim.fleet` and `ogsim.scada` only (not used by
`ogsim.market`/`ogsim.control`, which have their own equivalents).

## Purpose

- `clock.py` -- injectable clock (`RealClock`, `FakeClock`) so fleet/scada
  tests never use wall-clock sleeps.
- `jcs.py` -- RFC 8785 JSON Canonicalization Scheme serializer.
- `crypto.py` -- Ed25519 signature verification per `interfaces/crypto.md`.
- `schemas.py` -- loads/caches `interfaces/mqtt/*.schema.json` and validates
  outbound messages.
- `mqtt_client.py` -- thin `aiomqtt` wrapper: schema-validated publish,
  batched publish, subscribe. Depends on a small `MqttTransport` protocol so
  unit tests can stub the broker.
- `config.py` -- YAML (`OGSIM_FLEET_CONFIG`/`OGSIM_SCADA_CONFIG`) + env
  (`OG_MQTT_HOST`/`OG_MQTT_PORT`/`OG_MQTT_ROOT`/`OG_MQTT_SIM_PASSWORD`)
  configuration dataclasses, mirroring `ogsim.market.config`'s style.

## Interface

Import from `ogsim.common.<module>`. Never import `opengrid` or `ogsim.market`/
`ogsim.control` from here.

## How to test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -k "clock or jcs or crypto or schema or mqtt_client or config" -q
```
