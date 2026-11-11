# Security and privacy

Do not post credentials, real IP addresses, network configuration, unredacted logs,
account details, or local user paths in public issues.

DualNet reads adapter information and traffic counters locally. Ping probes go to
1.1.1.1 and can fall back to 8.8.8.8. TUN DNS uses 1.1.1.1. The first TUN activation
downloads pinned upstream runtime archives over HTTPS and verifies SHA-256 before
installing them. Runtime binaries already present in engine/bin are trusted local
files; do not replace them with files from untrusted sources.

The app does not include an analytics uploader. Runtime logs and generated TUN
configuration may contain sensitive network details. These, UI preferences, build
output, and executable backups are excluded from source control and public packages.
The proxy listens only on loopback and offers no authentication for local clients.
It is not intended to provide a service to other devices.

For a suspected vulnerability, use the repository's private vulnerability reporting
feature if available. Otherwise open an issue requesting a private contact channel
without publishing exploit details or sensitive data.
