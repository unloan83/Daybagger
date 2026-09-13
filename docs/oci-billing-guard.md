# OCI Billing Guard

The guard queries OCI's tenancy-wide Usage API every 15 minutes. If positive
cost lines total at least the configured threshold, it writes a persistent
lockdown marker, stops the VM2 research slice, and requests that OCI stop VM2.

This is a loss limiter, not a zero-cost guarantee. OCI usage data can arrive
late, budget alerts are soft limits, stopping VM2 does not remove charges from
detached volumes or unrelated services, and the guard cannot run after its VM
has stopped. Preventive OCI quotas and least-privilege IAM remain mandatory.

## Safety behavior

- Enforcement defaults to disabled.
- Configuration is parsed as data and is never sourced as shell code.
- Only positive cost lines count; negative credits cannot conceal them.
- A positive cost in an unexpected currency is an error and fails closed once
  enforcement is enabled.
- Loss of Usage API visibility stops the research slice when enforcement is
  enabled, but does not stop the VM by default.
- No volume, instance, backup, network, or database is ever deleted.
- Unlocking is manual: investigate the charge before removing
  `/var/lib/daybagger-billing-guard/LOCKDOWN.json`.

## OCI IAM prerequisites

Create a dynamic group containing only VM2:

```text
ALL {instance.id = '<VM2_INSTANCE_OCID>'}
```

Grant only cost-read and self-power-action access. Confirm the policy syntax in
OCI Policy Builder for the tenancy's identity-domain configuration:

```text
Allow dynamic-group DaybaggerBillingGuard to read usage-reports in tenancy
Allow dynamic-group DaybaggerBillingGuard to use instances in tenancy where target.instance.id = '<VM2_INSTANCE_OCID>'
```

The first permission reads cost data for the whole tenancy. The second allows
power actions on VM2 without allowing it to create or terminate instances.

## Activation gate

Install the files, but do not enable the timer until this command returns
`PREFLIGHT_OK` and the currency matches the tenancy's billing currency:

```bash
sudo PYTHONPATH=/opt/daybagger-research \
  /opt/daybagger-admin/venv/bin/python \
  -m daybagger.operations.oci_billing_guard \
  --config /etc/daybagger/billing-guard.env \
  --preflight
```

Then set `ENFORCEMENT_ENABLED=true`, run preflight again, and enable the timer:

```bash
sudo systemctl enable --now daybagger-billing-guard.timer
systemctl list-timers daybagger-billing-guard.timer
```

Research and backtest processes should run inside
`daybagger-research.slice`. If a breach occurs, the slice is stopped before the
OCI self-stop request is submitted.

## Recovery

1. Use Cost Analysis to identify and stop/delete the actual billable resource.
2. Verify current cost and forecast in OCI.
3. Confirm the VM remains within Always Free aggregate limits.
4. Start VM2 manually if it was stopped.
5. Run guard preflight.
6. Remove the lockdown file only after human review.
7. Restart the timer and research workloads deliberately.

## OCI limitations

Oracle documents the Usage API as the data source used by Cost Analysis.
Budgets are soft limits and their alerts are periodic, not transactional. OCI
IAM policy is required to read tenancy usage reports, and `use` access on
instances includes power actions such as stop/start.

- [Usage API](https://docs.oracle.com/en-us/iaas/tools/python/latest/api/usage_api/client/oci.usage_api.UsageapiClient.html)
- [Cost Analysis IAM](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costanalysisoverview.htm)
- [Budgets](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm)
- [Compute instance power permissions](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/instances.htm)
