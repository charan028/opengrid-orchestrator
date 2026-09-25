# OpenGrid wire crypto: Ed25519 command/verdict signing

Authoritative source: `02a-mvp-s-spec-engine.md` §6.4 (Ed25519 signing format), §8.2 (trace hash chain);
`02b-mvp-s-spec-platform.md` §6.3 (MQTT message schemas), §1.5 (key provisioning).

This document lets an independent implementation (the integration simulators, `ogsim`) sign and verify
commands without importing any Python from the orchestrator.

## 1. Algorithm

- **Signature scheme:** Ed25519 (RFC 8032), 32-byte seed / 32-byte public key / 64-byte signature.
- **Canonicalization:** RFC 8785 JSON Canonicalization Scheme (JCS) — sorted object keys (by UTF-16 code
  unit), no insignificant whitespace, numbers serialized per JCS `ECMAScript`-compatible rules, strings
  escaped per JCS. Every hash or signature in this system is computed over the JCS bytes (UTF-8) of a JSON
  value, never over a language-specific `repr()` or a non-canonical `json.dumps()`.
- **Encoding for wire fields:** signatures and hashes appear as strings.
  - `signature` fields: **base64url**, no padding (`base64.urlsafe_b64encode(sig).rstrip(b"=")`).
  - `hash` / `inputs_hash` / `prev_hash` fields: **hex-encoded SHA-256** (lowercase, 64 chars), not base64.
  - `key_id`: an opaque short string identifying which Ed25519 public key was used (e.g.
    `guardian-2026a`, `safestop-2026a`). Verifiers keep a `key_id -> public_key` map; MVP-S has exactly one
    active `guardian` key and one active `safestop` key (§6.5 of `02a`: distinct keys, the safestop key can
    only sign `ENGAGE`, never `RELEASE`).

## 2. What is signed

### 2.1 Command batch envelope (`og/v1/cmd/<bank_id>/batch`)

The guardian signs the JCS bytes of the batch object **with the `signature` (and `key_id`) fields
excluded**:

```
signing_input = JCS({
  "batch_id": <uuid string>,
  "bank_id": <string>,
  "epoch": <integer>,
  "seq": <integer>,
  "issued_at": <RFC 3339 UTC timestamp, "...Z">,
  "expires_at": <RFC 3339 UTC timestamp, "...Z">,
  "items": [ {"hub_id": ..., "p_kw_setpoint": ..., "reason_code": ...}, ... ]
})
signature = base64url(Ed25519_sign(guardian_private_key, signing_input))
```

The published envelope adds `signature` and `key_id`:

```json
{
  "batch_id": "0190f7b0-...",
  "bank_id": "bank-07",
  "epoch": 42,
  "seq": 1183,
  "issued_at": "2026-09-26T18:00:02.104Z",
  "expires_at": "2026-09-26T18:00:12.104Z",
  "items": [{"hub_id": "hub-0007", "p_kw_setpoint": -3.2, "reason_code": "SELECTOR"}],
  "key_id": "guardian-2026a",
  "signature": "base64url..."
}
```

A verifier (hub, simulated or real) recomputes `signing_input` from the received object (dropping
`signature`/`key_id`, re-serializing the remaining fields with JCS) and calls
`Ed25519_verify(guardian_public_key[key_id], signing_input, signature)`. `epoch`/`seq`/`lease`/`valid_from`
(`issued_at`)/`expires_at` freshness checks (K6/G-13) happen **after** signature verification and are
independent of it: an out-of-order or stale command is rejected even if correctly signed.

### 2.2 Guardian verdict (`SignedVerdict`, internal engine↔guardian, not on MQTT)

```
signing_input = JCS({
  "command_batch_id": <uuid>, "outcome": <PASS|PARTLY_VETOED|VETOED|TIMEOUT>,
  "vetoed_rule_ids": [...], "inputs_hash": <hex sha256>, "signed_at": <timestamp>
})
signature = base64url(Ed25519_sign(guardian_private_key, signing_input))
```

`inputs_hash` is itself `sha256_hex(JCS({"version_vector": ..., "ledger_version": ..., "batch_hash": ...}))`
— a domain-separated hash over the arbitration inputs the verdict was computed from (RT-008).

### 2.3 Stop event (`og/v1/stop/<scope>/<id>`, retained)

Signed the same way, over the JCS bytes of the `StopEvent` fields excluding `signature`/`key_id`. Only the
`safestop` key may sign `action="ENGAGE"`; `action="RELEASE"` must be signed by the `guardian` key after
Tier-2 (two-person) approval — a verifier rejects a `RELEASE` signed by the `safestop` key.

### 2.4 Trace hash chain (not Ed25519 — SHA-256 only; see `interfaces/mqtt/topics.md` note)

Trace records are hash-chained, not signed. `record_hash = sha256_hex(JCS(header))` where `header` includes
`prev_hash`. See `orchestrator/src/opengrid/core/tracehash.py` (owner: architect) for the canonical
implementation; simulators never need to reproduce trace hashing, only command/verdict/stop signing above.

## 3. Key provisioning (reference only, no secret values here)

- `GUARDIAN_SIGNING_SEED` (env, `secrets.env`) — 32-byte Ed25519 seed, base64 or hex depending on the
  loader; the guardian process derives its keypair from it at startup and publishes only the public key
  (out of band, to `ogsim`'s config) for verification.
- A simulator that needs to *verify* guardian signatures is given the guardian's public key via its own
  test/dev config (`ogsim` config, not a secret) — never the private key.
- A simulator that needs to *emit* signed messages on behalf of a fake guardian (for negative testing, e.g.
  "tampered/unsigned command must be rejected") generates its own Ed25519 keypair and either (a) signs with
  the wrong key to produce a `BAD_SIGNATURE` case, or (b) omits `signature` entirely to produce an unsigned
  case. Real orchestrator hubs must reject both.

## 4. Reference vectors (worked example, for cross-implementation testing)

Given the trivial payload `{"a": 1, "b": 2}`, JCS serializes to the byte string `{"a":1,"b":2}` (UTF-8, no
spaces, keys already sorted). Implementations should confirm their JCS layer produces exactly this byte
string before trusting it for signing; `orchestrator/tests/unit/core/test_crypto.py` and
`test_tracehash.py` pin down further vectors including key-order shuffling, nested objects, and Unicode
strings.
