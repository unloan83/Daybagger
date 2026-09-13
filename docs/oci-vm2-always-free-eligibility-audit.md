# OCI VM2 Always Free Eligibility Audit

## Executive determination

The new research VM meets every instance-level technical condition that could be
verified independently for OCI's current Always Free Ampere A1 allocation:

- It is `VM.Standard.A1.Flex`.
- It is provisioned at 1 OCPU and 6 GB memory.
- It runs in `ap-mumbai-1`.
- OCI's authenticated region-subscription API identifies `ap-mumbai-1` as this
  tenancy's home region.
- Its only guest-visible block device is approximately 50.04 GB.
- It runs Oracle Linux Server 9.8 on `aarch64`, an eligible operating-system
  family for Always Free A1 compute.

**Determination: technically Always Free eligible, but not yet billing-certified.**
The VM's own allocation is safely inside the current Always Free limits. A final
zero-cost certification cannot be made from instance metadata alone because OCI
IAM denies this instance permission to list the tenancy's other A1 instances,
detached boot/block volumes, volume backups, budgets, and billing records. The
local `DAYBAGGER_OCI_ACCOUNT_TYPE=FREE_TIER` value records intent; it is not
authoritative billing evidence.

If the OCI Console shows that this tenancy is an unupgraded Always Free account,
there are no other A1 allocations, total home-region boot/block volumes remain
at or below 200 GB, and the VM/volume show Always Free eligibility, this VM is
within the published $0 allocation. Oracle describes Always Free resources as
free for the life of the account, but the term "forever" should not be treated
as an unconditional guarantee: program terms can change and idle instances may
be reclaimed.^1

## Evidence and eligibility matrix

| Condition | Current Oracle rule | Live VM2 evidence | Result |
|---|---:|---:|---|
| Compute shape | `VM.Standard.A1.Flex` | `VM.Standard.A1.Flex` | Pass |
| A1 tenancy allocation | 1,500 OCPU-hours and 9,000 GB-hours monthly; equivalent to 2 OCPUs/12 GB continuously | 1 OCPU/6 GB on this VM | Pass for this VM; tenancy aggregate unverified |
| Region | Always Free compute must be in the tenancy home region | Running region and authenticated home region are both `ap-mumbai-1` | Pass |
| Operating-system family | Oracle Linux and Ubuntu are listed as eligible A1 image families | Oracle Linux Server 9.8, ARM64 | Pass at family level; Console label unverified |
| Boot/block storage | 200 GB total in the home region, shared by boot and block volumes | One guest-visible ~50.04 GB boot device | Pass for attached device; tenancy aggregate unverified |
| Volume backups | Five total Always Free volume backups | Not visible from guest; API denied | Unverified |
| Subscription type | Free account is not charged unless deliberately upgraded | `.env.local` says `FREE_TIER` | Unverified; Console is authoritative |
| Cost/budget state | Cost data and budget rules require billing IAM access | Instance-principal access denied | Unverified |
| Other paid resources | Must be checked separately | No tenancy resource inventory permission | Unverified |

Oracle's current documentation assigns each tenancy 1,500 A1 OCPU-hours and
9,000 memory GB-hours per month, equivalent to 2 OCPUs and 12 GB memory for an
Always Free tenancy.^1 The same documentation assigns 200 GB total home-region
boot/block storage and five volume backups.^1

## Monthly allowance calculation

For the longest ordinary billing month of 31 days:

| Resource | Calculation | VM2 maximum | Published free allowance | Remaining if VM2 is the only A1 instance |
|---|---:|---:|---:|---:|
| OCPU-hours | 1 OCPU × 24 × 31 | 744 | 1,500 | 756 |
| Memory GB-hours | 6 GB × 24 × 31 | 4,464 | 9,000 | 4,536 |
| Boot/block capacity | Live disk size | ~50.04 GB | 200 GB | ~149.96 GB |

The 1 OCPU/6 GB configuration consumes about 49.6% of each monthly A1 compute
allowance in a 31-day month. Actual CPU utilization does not change allocated
OCPU-hours; resizing or creating another A1 instance changes the calculation.
Two continuously running 1 OCPU/6 GB A1 instances would consume 1,488
OCPU-hours and 8,928 GB-hours in a 31-day month, still just inside the published
allowance, but with little margin.

The new VM and the existing AMD VM report different tenancy OCIDs. Therefore,
the existing VM's E2 Micro compute and storage do not consume the new tenancy's
A1 or block-volume allowances. Each tenancy must nevertheless be checked on its
own.

## Evidence obtained directly from OCI

The following was obtained from OCI Instance Metadata Service v2 and an
authenticated instance-principal call on the new VM:

| Property | Verified value |
|---|---|
| Shape | `VM.Standard.A1.Flex` |
| Provisioned OCPUs | 1 |
| Provisioned memory | 6 GB |
| Architecture | `aarch64` |
| Running region | `ap-mumbai-1` |
| Authenticated tenancy home region | `ap-mumbai-1` |
| Subscribed regions | 1 |
| Availability domain | `AP-MUMBAI-1-AD-1` |
| Guest-visible block devices | One |
| Guest-visible block capacity | 50,036,998,144 bytes |
| OCI cloud agent | Active |

The instance OCID, compartment OCID, region, and root-tenancy relationship also
match the protected Daybagger `.env.local` values. OCIDs and IP addresses are
excluded from this report.

## What metadata cannot prove

An A1 shape does not by itself prove a $0 bill. Paid accounts can run the same
shape, and only the portion within the aggregate allowance is free. Oracle also
states that eligible resources are identified in the Console and that resources
outside the home region can incur charges.^1

The VM's instance principal exists, but OCI returned authorization errors for:

- listing compute instances across the tenancy;
- reading Limits, Quotas and Usage;
- listing boot and block volumes;
- listing budgets and budget alert rules.

Consequently, the audit cannot see an orphaned boot volume, detached block
volume, excess backup, second A1 VM, paid service, or the account's upgrade
status. Guest commands such as `lsblk` and `df` can only show attached devices;
they are not tenancy billing records.

## Subscription-status test

The OCI Console provides a direct account-type distinction:

- If **Upgrade and Manage Payment** opens a page titled **Upgrade**, Oracle says
  the account is Free Tier/Always Free.
- If the page title is **Manage Payment**, the account has already been upgraded.
- On an Always Free account, Oracle says the billing details widget shows no
  billing or usage information and offers an **Upgrade account** action.^2

Oracle states that the signup card is not charged unless the account is
upgraded.^3 This is the strongest protection against accidental billing. On a
Pay As You Go account, eligible usage remains free, but usage above allowances
can be billed; IAM and quotas must therefore prevent additional resources.

## Required Console verification

The following checks are necessary to elevate the determination from
"technically eligible" to "billing-certified":

1. Open **Profile → Upgrade and Manage Payment**. Record whether the page title
   is **Upgrade** or **Manage Payment**.
2. Open **Compute → Instances → VM2** and verify the A1 allocation is 1 OCPU and
   6 GB and that the resource/shape is shown as Always Free eligible.
3. Open **Governance & Administration → Limits, Quotas and Usage** in
   `ap-mumbai-1`. Confirm aggregate A1 allocation does not exceed 2 OCPUs and
   12 GB.
4. Open **Storage → Block Storage → Boot Volumes** and **Block Volumes** in every
   compartment in `ap-mumbai-1`. Confirm combined capacity is at most 200 GB.
5. Check boot-volume backups and block-volume backups. Keep the combined count
   at five or fewer.
6. Open VM2's boot volume and confirm its capacity is approximately 50 GB and
   its performance setting has not been raised above the default Balanced
   level. OCI sells higher performance separately.^4
7. If the page is available, open **Billing & Cost Management → Cost Analysis**,
   filter by VM2's resource OCID, and verify computed cost is zero after usage
   data has populated.^5
8. Review the tenancy for chargeable resources not implied by this VM: extra
   public/reserved IP resources, NAT gateways, paid load balancers, databases,
   object storage above allowance, custom images, and cross-region volumes.

## Budget and hard-guardrail assessment

A budget is not a spending cap. Oracle calls budgets soft limits; alert rules
are evaluated periodically, normally every 24 hours.^6 A $1 monthly budget with
a 1% actual-spend alert approximates a $0.01 warning threshold, but it cannot
prevent the first charge and may notify after cost has already accrued.

The attempted budget audit returned an OCI authorization error, so no budget or
alert was created. Granting budget-management permission to a workload VM would
unnecessarily enlarge its privileges. Prefer configuring the alert through an
administrator identity or the Console.

For a paid tenancy, the stronger controls are:

- a dedicated compartment for VM2;
- compartment quotas limiting A1 compute and memory to the intended allocation;
- quotas denying nonapproved compute families and paid managed services;
- least-privilege IAM preventing VM2 and ordinary users from creating or
  resizing infrastructure;
- a small budget with both actual and forecast alerts;
- daily review of cost/usage reports during the first week.

Quotas constrain resource consumption, while budgets only notify.^7 Neither
replaces verification of the subscription model and actual cost records.

## Operational and cost risks

| Risk | Current state | Severity | Control |
|---|---|---:|---|
| Another A1 VM exists in the new tenancy | API denied | High | Verify aggregate compute in Console |
| Detached/orphaned volume exists | API denied | High | Review all compartments and volumes |
| Account is Pay As You Go | Only local assertion exists | High | Verify page title and subscription details |
| Boot volume performance exceeds default | API denied | Medium | Check volume VPUs in Console |
| Paid service exists elsewhere in tenancy | API denied | High | Review Cost Analysis and tenancy inventory |
| Idle A1 reclamation | VM is currently lightly provisioned | Availability risk, not billing risk | Monitor useful workload; do not generate artificial activity |
| Egress from research results | Expected to be small | Low | Monitor monthly outbound transfer |
| Budget alert absent | Confirmed absent/unreadable | Medium | Configure through admin identity |

Oracle may reclaim an Always Free compute instance if CPU, network, and—for A1—
memory utilization all remain below specified thresholds over a seven-day
period.^1 Reclamation is an availability/data-retention risk, not evidence that
the instance is billable. Important research outputs should therefore be pushed
to reviewed, durable storage rather than existing only on VM2.

## Final conclusion

The new VM is correctly shaped and located to consume Always Free A1 capacity.
At 1 OCPU, 6 GB memory, and approximately 50 GB of attached boot storage, it is
well inside the per-tenancy limits **if it is the only A1 allocation and there
are no unseen volumes**. The home-region requirement is independently proven,
not assumed.

The defensible status is:

> **Always Free eligible: yes. Confirmed zero-bill tenancy status: not yet.**

Only the OCI Console or an administrator-authorized inventory and billing query
can close the remaining aggregate-usage and subscription-status gaps. Until
those checks pass, do not resize VM2, add storage, create backups, or start
high-egress data pipelines. Heavy CPU work at the current fixed allocation does
not itself exceed the compute allowance, but infrastructure changes can.

## Sources

1. Oracle. [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm). Accessed September 13, 2026.
2. Oracle. [Viewing Billing Details](https://docs.oracle.com/en-us/iaas/Content/GSG/Concepts/console_topic-AccountCenter-Billing.htm). Accessed September 13, 2026.
3. Oracle. [Oracle Cloud Infrastructure Free Tier](https://docs.oracle.com/iaas/Content/FreeTier/freetier.htm). Updated June 29, 2026.
4. Oracle. [Block Volume Performance](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/blockvolumeperformance.htm). Accessed September 13, 2026.
5. Oracle. [Cost Analysis](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costanalysisoverview.htm). Accessed September 13, 2026.
6. Oracle. [Budgets](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm). Updated February 12, 2026.
7. Oracle. [Quota Policy Syntax](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/quota_policy_syntax.htm). Updated August 24, 2026.
8. Live VM2 audit using OCI Instance Metadata Service v2, OCI Python SDK instance-principal authentication, and operating-system telemetry. September 13, 2026. Private operational evidence.
