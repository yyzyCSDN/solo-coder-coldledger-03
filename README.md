# ColdLedger Pharmaceutical Cold-Chain Operations Lab

Dependency-free Python reference implementation for temperature-sensitive pharmaceutical distribution. It models inventory expiry, chain-of-custody, telemetry correction, packaging qualification, dry-ice depletion, freezer allocation, customs holds, serialization, recalls, release decisions, vehicle compartment compatibility, returns, SLA calendars and cross-dock state.

The `recall` package tracks a recall case end to end: append-only scope and regulatory-deadline revisions, notification waves, de-duplicated customer acknowledgments, escalations pinned to the deadline revision they were decided under, per-customer returned-quantity reconciliation, and a hashed closure report with a verifiable closure certificate.

Runtime third-party dependencies: none.
