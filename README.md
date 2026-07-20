# Avalon Q Supervisor

[![Tests](https://github.com/Jnapier2/avalon-q-supervisor/actions/workflows/test.yml/badge.svg)](https://github.com/Jnapier2/avalon-q-supervisor/actions/workflows/test.yml)

Avalon Q Supervisor is a local-first operations layer for a Canaan Avalon Q. It converts the device's TCP telemetry into clear health states, applies a durable restart budget, and records privacy-conscious evidence for each decision.

The project shows how small-scale hardware operations can be made easier to review and safer to automate. Instead of treating every anomaly as a reboot request, it separates transient degradation, recoverable critical conditions, offline devices, and conditions that need an operator.

## Operational value

- **Decision-ready status:** normalizes `summary`, `estats`, `pools`, and `version` responses into `healthy`, `degraded`, `critical`, `offline`, or `unknown` states.
- **Bounded recovery:** reloads, checks, and reserves a durable reboot budget under one cross-process lock, after repeated critical evidence and before command transmission.
- **Safety by design:** runs in dry-run mode unless recovery is enabled in configuration *and* `--execute` is supplied at runtime. Thermal events, missing telemetry, and unverified device identity remain manual-intervention conditions.
- **Auditable without exposing operations:** persists a sanitized recovery-intent event before reservation, then records the command outcome. Raw responses and operational identifiers are excluded.
- **Portable validation:** uses only the Python 3.10+ standard library and includes deterministic fixtures that run without mining hardware.

Configuration is centralized in one reviewed file, with environment and command-line overrides for deployment-specific values. That makes thresholds and control limits visible instead of scattering them through recovery logic.

## Try the hardware-free demo

```powershell
Copy-Item config.example.json config.local.json
python -m avalon_q_supervisor --config config.local.json demo
```

The fixture walks through healthy operation, repeated recoverable failures, a dry-run reboot decision, and recovery. It writes the same JSONL evidence used by live checks to `var/audit.jsonl`.

Run the regression suite:

```powershell
python -m compileall -q avalon_q_supervisor tests
python -m unittest discover -s tests -v
```

## Live use

Copy `config.example.json` to an untracked local file, replace the documentation-only host, and set an expected hash rate appropriate to the device and operating mode. A live one-time check remains non-mutating:

```powershell
$env:AVALON_Q_HOST = "<private-ip-on-your-local-network>"
$env:AVALON_Q_EXPECTED_HASHRATE_THS = "your-reviewed-target"
python -m avalon_q_supervisor --config config.local.json check
```

For continuous observation:

```powershell
python -m avalon_q_supervisor --config config.local.json watch --interval 30
```

Actual reboot recovery has two independent gates:

1. Set `recovery.enabled` to `true` in the untracked local configuration.
2. Add `--execute` to `check` or `watch`.

The command sent after all gates and limits pass is Canaan's documented `ascset|0,reboot,0`. Before transmission, the supervisor verifies the configured model, writes recovery intent, reloads state under an interprocess lock, and durably reserves budget. A failed or interrupted attempt therefore cannot create an immediate retry burst.

## Configuration and secrets

`config.example.json` uses the reserved documentation address `192.0.2.10`; it is demo-only and live commands reject it. The configured `expected_model` defaults to Canaan's documented `Q` model identifier. The example pool endpoint is the current CKPool automatic-routing endpoint, `stratum+tcp://stratum.ckpool.org:3333`. Replace it if a different service is intended; it is used only to flag a mismatch and is never written to the audit log.

Supported environment overrides:

- `AVALON_Q_HOST`
- `AVALON_Q_PORT`
- `AVALON_Q_EXPECTED_HASHRATE_THS`
- `AVALON_Q_EXPECTED_POOL_URL`
- `AVALON_Q_RECOVERY_ENABLED`
- `AVALON_Q_AUDIT_LOG`
- `AVALON_Q_STATE_FILE`
- `AVALON_Q_AUDIT_SALT` for a stable, salted device pseudonym

Live commands accept only a literal RFC 1918 private, loopback, link-local, or IPv6 unique-local address. Public addresses and hostnames fail closed before a socket is opened.

The state and audit paths must be different. Before enabling recovery, place both on a local filesystem rather than a network share or cloud-synchronized folder; cross-host and sync-provider locking semantics are outside this project's boundary. Invalid, unreadable, or non-finite configuration, telemetry, fixtures, or recovery state fails closed.

This repository does not store or request web-admin credentials, pool workers, wallet addresses, or pool passwords. The adapter intentionally limits state-changing scope to the exact reboot command used by the bounded policy.

## Validation boundary

The parser and adapter are validated against Canaan's published Avalon Q command format, synthetic protocol responses, bounded/truncated transport fixtures, and a deterministic supervision sequence. Each request uses a monotonic total deadline and requires a complete protocol frame. Missing hash rate, pool, temperature, or verified model identity blocks automatic recovery. The suite also exercises simultaneous processes against one recovery budget on Windows-compatible and Linux-compatible locking paths.

No live Avalon Q was reachable from the publication environment. Device-specific network behavior and an executed reboot must still be validated by the operator on an isolated local network before recovery is enabled.

Protocol references: [Canaan Avalon Q commands](https://www.canaan.io/resource/api/avalon-q-commands), [Canaan local-only remote-control guidance](https://support.canaan.io/en-us/knowledgebase/article/KA-01254), and [CKPool endpoint documentation](https://solo.ckpool.org/).

## Scope

This is an operational reliability demonstration, not a profitability model or mining recommendation. It does not install services, discover devices, change pool settings, expose a web server, contact cloud control planes, or weaken device/network security.

Copyright © 2026 Gateway Information Group LLC. All rights reserved. See [LICENSE.md](LICENSE.md).
