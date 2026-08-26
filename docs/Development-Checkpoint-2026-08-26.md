# PPS Development Checkpoint — 2026-08-26

## 1. Release State

- Branch: `feature/job-workspace`
- Local HEAD: `27a26f8beb0b6a65e590e6399a88660371394f84`
- Remote HEAD: `27a26f8beb0b6a65e590e6399a88660371394f84`
- Ahead/behind: `0 / 0`
- Latest release commit: `27a26f8 Fix mobile invoice total wrapping`
- Recent important commits:
  - `47172a3 Simplify and polish PPS operator interface`
  - `86fa39e Fix fulfillment document manifest paths`
  - `8f91177 Complete universal sourcing redesign`
  - `beb2f4d Fix portable document manifest paths`

## 2. Production Acceptance

- Universal sourcing redesign accepted.
- Targeted simplicity cleanup accepted.
- Visual polish upgrade accepted.
- Mobile invoice wrapping hotfix accepted.
- Final live production result: **PASS**.

The production server does not expose a Git SHA. The deployed hotfix behavior was directly observed in production: the invoice page returned HTTP 200 at 390×844, `$702.00` and `$568.16` remained on one line, the page had no horizontal overflow or clipped controls, and the representative invoice PDF returned HTTP 200 with `application/pdf`.

## 3. Current Product Model

```text
Customer
→ Sourcing Job
→ Requested Need
→ Research
→ Quote
→ Payment
→ Order
→ Receiving
→ Delivery
→ Closeout
```

- Requested Need is the primary sourcing identity.
- Asset / Equipment Context is optional, secondary, and conditional.
- Partial receiving and partial delivery are supported.
- PPS remains the system of record.
- Operator confirmation remains authoritative.

## 4. Current UI State

The accepted operational priority is: Job identity → Payment → Ordering → Receiving → Delivery → Next Action.

- Operational history is compact and secondary to current state.
- Actions use precise operator wording and a clear primary/secondary hierarchy.
- Terminology works for both general sourcing and parts workflows.
- Desktop and mobile presentation is polished and responsive.
- Quote and invoice item blocks respond cleanly on mobile.
- Document Center integrity visibility and filtering are retained.

## 5. Data Safety Baseline

- Authoritative DB SHA-256: `bb3664afc1a21c030b253cc4fb2dfc433e84d3132af319764514abd1fd5543a9`
- Trusted logical DB fingerprint: `6b6a94e5972943354497d547214c8c8d610c4ddb5350d0c1961982c701f7b3d7`
- Document-tree fingerprint: `9bf16a63c83f5928722649354ec064b1e26d1592899ce9fbcc5640267f84a213`
- `PRAGMA integrity_check`: `ok`
- `PRAGMA foreign_key_check`: clean (no rows)
- `audit_logs`: 134 rows; maximum ID 164

## 6. Test Baseline

Fresh full nonbrowser run on 2026-08-26 using a disposable database, document root, and upload root:

- 648 tests passed.
- 192 subtests passed.
- 10 existing FastAPI lifespan deprecation warnings.
- 0 failures.

## 7. Document Integrity State

- Quote and invoice portable manifest-path handling is active.
- Supplier-order, receiving, and delivery portable manifest-path handling is active.
- Representative live supplier purchase-order, receiving-summary, delivery-note, quote, and invoice PDFs passed read-only production checks.
- No known current Document Center integrity issues remain from the latest live acceptance.

## 8. Production QA Access

- Cloudflare Access service-token QA is configured.
- Credentials are inherited through environment variables and must never be printed.
- Browser QA injects Access headers only for the exact `api.pinpointsourcing.com` origin.
- Production QA remains GET/read-only unless a future task explicitly authorizes a write.

## 9. Known Unrelated Local Files

The following untracked paths were present at this checkpoint:

```text
Pinpoint Sourcing Brand Identity Board.png
artifacts/
codex_batch1_final_report.md
codex_batch1_staging_report.md
codex_batch2_design_report.md
codex_batch2a_implementation_report.md
codex_batch2b_implementation_report.md
codex_batch3a_implementation_report.md
codex_batch3b_implementation_report.md
codex_batch3c1_implementation_report.md
codex_batch3c_implementation_report.md
codex_batch3d_implementation_report.md
codex_batch3e_implementation_report.md
div
plg_core/application.py.roadmap-backup
roadmap_current_code.txt
scripts/reconcile_internal_invoice_documents.py
span
span,
```

These files are outside the accepted PPS release and were intentionally left untouched.

## 10. Current Development Status

**PRODUCTION-ACCEPTED CHECKPOINT**

No known release-blocking issue remains from:

- universal sourcing conversion
- fulfillment document manifest paths
- simplicity cleanup
- visual polish
- mobile invoice total wrapping

## 11. Next Development Boundary

Future development must begin from this checkpoint and preserve:

- accounting data
- operational data
- issued documents
- partial fulfillment
- universal sourcing model
- operator confirmation boundaries

No next feature is assumed or invented in this checkpoint.
