# Current Sprint

## Release

PLG Alpha 8.3

## Current Feature

Revenue Adjustments

## Business Goal

Prevent missed revenue by allowing job-level Service Charges and Sourcing Fees.

## Completed

- Revenue Adjustments business model approved.
- Charges assigned to the Job.
- Migration `0008_job_revenue_adjustments` created.
- Database fields added:
  - `service_charge`
  - `service_charge_description`
  - `sourcing_fee`
  - `sourcing_fee_description`
- Migration tested, committed, and pushed.

## Current Task

Build the Revenue Adjustments panel in the Job Command Center.

The first UI milestone must:

- display current job values;
- allow editing;
- validate Service Charge as either $0 or at least $150;
- allow a nonnegative Sourcing Fee;
- save descriptions;
- reload saved values;
- avoid changing Quote or Invoice calculations yet.

## Next Tasks

1. Revenue Adjustments panel and save route.
2. Pre-quote Revenue Checklist.
3. Sourcing Fee recommendation logic.
4. Quote calculation integration.
5. Quote PDF integration.
6. Invoice calculation integration.
7. Invoice PDF integration.
8. Business Intelligence reporting.

## Current Stable Commit

`c99ad17` — PLG Alpha 8.3 job revenue adjustments foundation

## Developer Safety Tool

Before continuing Revenue Adjustments polish, build the read-only Developer Startup Tool.

Official command: `python3 -m plg_core.devtools.startup`

The tool must report documentation health, the current sprint, Git status, branch synchronization, and whether a checkpoint is required.
