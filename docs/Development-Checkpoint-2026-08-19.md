# Development Checkpoint — 2026-08-19

Documentation-only checkpoint. No application behavior, schema, or production data was changed by creating this file.

## Completed today

- Corrected the ChatGPT/Firefox Smart Intake bridge to use the canonical PPS Inbox endpoint.
- Added review-only research evidence to Smart Intake and preserved it as untrusted, unselected Job research after operator confirmation.
- Improved the Smart Intake review screen while retaining Customer, Machine/Asset, and Requested Need confirmation boundaries.
- Added simplified quantity-aware fulfillment using existing invoice items, supplier orders, receipts, and deliveries.
- Added partial receiving and delivery controls and progress to the Job Command Center.
- Simplified the Job Command Center presentation without removing existing information or workflows.
- Added presentation-only custom invoice adjustments and retained the rule hiding Service Charge and Sourcing Fee on custom customer invoices.
- Added authenticated, explicit ChatGPT/Firefox Job actions for payment, order placement, item receiving, and item delivery.
- Preserved accounting/source-record immutability outside the existing authoritative payment workflow and retained audit/idempotency behavior.

## Remaining

- Add `pps:firefox:jobs:update` to the intended local/deployment bridge token configuration before using the new Job-update endpoints outside tests.
- Perform operator acceptance checks against disposable or approved Jobs for the natural-language-to-action workflow.
- Review and intentionally package/commit the existing dirty worktree; unrelated pre-existing changes and generated documents remain present.
- Deploy to Render only when explicitly authorized. No deployment was performed today.

## Test status

- Passing: 166 targeted Smart Intake, Inbox, fulfillment, receiving, delivery, Job Command Center, and ChatGPT/Firefox Job-action regression tests.
- Passing: isolated Dennis Brown / 1989 Mazda RX-7 Smart Intake submission returned HTTP 200, rendered its research evidence, remained DRAFT, and created no Job.
- Passing: partial quantity, invalid quantity, invalid Job, ambiguous item, audit, completion, and accounting-immutability coverage.
- Failing: none in the targeted suites.
- Not asserted by this checkpoint: the repository-wide test suite was not run in its entirety.
