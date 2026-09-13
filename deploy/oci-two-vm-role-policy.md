# OCI two-VM role policy

This deployment uses a strict one-way production boundary:

- **VM1 (`production`)** runs only DualEngine, Daybagger, and their telemetry.
  Backtests, historical-data pulls, universe reconstruction, pipeline debugging,
  and other research workloads must never run on VM1.
- **VM2 (`research`)** runs historical-data acquisition, cross-validation,
  universe reconstruction, backtests, and staging diagnostics. It must never run
  a live or paper decision-making copy of DualEngine or Daybagger.
- VM2 may emit result files and configuration recommendations only. A human must
  review them before production changes follow the normal Git commit, push, pull,
  and controlled VM1 service-restart process.
- Live credentials and `.env.local` must not be copied from VM1 or a developer
  workstation to VM2. Research credentials, when required, must be separately
  scoped and unable to place live orders.
- Heavy VM2 work is allowed only after its actual OCI shape and tenancy-wide
  storage consumption have been checked against current Always Free allowances.

The machine-readable role markers installed at `/etc/daybagger/vm-role.conf`
are maintained from `deploy/vm1-production.role` and
`deploy/vm2-research.role`.
