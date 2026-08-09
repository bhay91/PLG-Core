# Current Sprint

## Release

PLG Alpha 8.3

## Current Feature

Revenue Adjustments / Additional Charges

## Business Goal

Prevent missed revenue by allowing job-level Service Charges and Sourcing Fees and carrying them correctly through Job → Quote → Invoice.

## Completed and Verified

- Job-level Service Charge fields.
- Job-level Sourcing Fee fields.
- Descriptions for both charge types.
- Service Charge validation.
- Sourcing Fee validation.
- Additional Charges panel in the Job Command Center.
- `Service Charge Added` status.
- `Sourcing Fee Added` status.
- Save-success confirmation.
- Quote Summary revenue integration.
- Revenue Breakdown.
- Cost & Profit review.
- Service Charge included in Quote totals.
- Sourcing Fee included in Quote totals.
- Service Charge included in Invoice totals.
- Sourcing Fee included in Invoice totals.
- Quote PDF support.
- Invoice PDF support.
- Direct save test passed using a temporary database.
- Template and PDF integration checks passed.
- Customer-facing button renamed to `Save Additional Charges`.
- Obsolete milestone message removed.

## Current Task

Finalize Alpha 8.3 documentation, commit the verified work, and push the checkpoint.

## Next Task

After the Alpha 8.3 checkpoint is protected, continue the PPS full-product-audit improvement plan from the next highest-priority unfinished item.

## Current Working Commit

`26e77d1`

## Developer Safety Tool

Official command:

`python3 -m plg_core.devtools.startup`

Use it before development sessions to confirm documentation health, current Alpha, Git synchronization, and checkpoint status.

## Command Center Principle

The Job remains the operational command center. Additional Charges are stored on the Job and carried into downstream customer billing.
