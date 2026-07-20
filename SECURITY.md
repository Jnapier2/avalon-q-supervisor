# Security Policy

## Reporting

Please use GitHub private vulnerability reporting if it is enabled. Otherwise, contact the maintainer through the GitHub profile and request a private reporting channel. Do not place device addresses, credentials, wallet addresses, raw logs, firmware, or exploit details in a public issue.

Include the affected version, operating system, minimal sanitized reproduction, expected boundary, and observed behavior.

## Operating boundary

Run the supervisor only from a trusted host on the same isolated local network as the device. Live commands require a literal private, loopback, link-local, or IPv6 unique-local address; public addresses and hostnames are rejected before connection. Do not expose TCP port 4028 to the public internet. Canaan's guidance keeps remote control local to reduce centralized and unauthorized control risk.

Dry-run is the default. A state-changing reboot requires both `recovery.enabled: true` in an untracked configuration and the runtime `--execute` flag. It also requires a matching expected model, complete health telemetry, available audit storage, and valid durable recovery state. Review thresholds and test connectivity before enabling it. Temperature-critical states are never automatically rebooted.

Keep `config.local.json`, recovery state, audit logs, device exports, firmware, credentials, pool workers, and wallet information outside version control. Keep state and audit files at different paths on a local filesystem; do not rely on a network share or cloud-sync provider for lock coordination. Corrupt or unreadable state suppresses recovery until the operator inspects and repairs it.

Raw device responses may contain DNA, MAC, pool user, password, and network identifiers. The application normalizes them in memory, rejects invalid telemetry, and never writes the raw payload to its audit log. Recovery intent is written and synced before budget reservation or command transmission; a failed intent write blocks the command.
