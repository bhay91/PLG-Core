# Current Sprint

## Release

PPS Alpha 19

## Current Feature

Full Product Audit / Reconciliation

## Current Application Version

`1.0.0-alpha.19`

## Business Goal

Reconcile the PPS product that exists today with the approved operational workflow and full-product-audit plan.

The purpose is to identify real unfinished work, contradictions, duplicate systems, missing user-facing connections, and workflow gaps before adding more features.

## Previous Milestone

Alpha 8.3 — Revenue Adjustments / Additional Charges — COMPLETE

Service Charges and Sourcing Fees were verified through Job → Quote → Invoice and protected on GitHub.

## Existing Alpha 12–19 Capability Areas To Audit

- Quote lifecycle and approval.
- Invoice conversion and payment handling.
- Payment reversals and invoice voiding.
- Supplier purchasing.
- Receiving and partial receiving.
- Customer delivery.
- Customers and Machine Registry.
- Global search.
- Dashboard and operational follow-up.
- Accounting and audit history.
- Readiness and API hardening.

## Current Task

Audit the implemented Alpha 12–19 capabilities against the actual PPS user workflow and identify the highest-priority unfinished or disconnected milestone.

Do not build a new feature until the audit identifies a practical gap.

## Audit Principles

- Job remains the operational command center.
- Prefer completing or connecting existing capability over rebuilding it.
- Every change must save time, reduce mistakes, protect revenue, or support the real PPS workflow.
- Avoid speculative features.
- Make small verified changes.
- Protect stable checkpoints before moving forward.

## Current Working Commit

`f80d4fd`

## Developer Safety Tool

Official command:

`python3 -m plg_core.devtools.startup`

Use it before development sessions to verify documentation health, phase, Git state, and checkpoint status.
