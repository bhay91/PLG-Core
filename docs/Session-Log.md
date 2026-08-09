# PLG Development Session Log

## 2026-08-06 — Revenue Adjustments

### Current Release

PLG Alpha 8.3

### Completed and Verified

- Documentation foundation saved to GitHub.
- Revenue Adjustments database fields created.
- Revenue Adjustments save route added.
- Service Charge validation added.
- Sourcing Fee validation added.
- Revenue Adjustments panel added to Job Command Center.
- Service Charge saved and reloaded at $150.
- Sourcing Fee saved and reloaded at $75.
- Descriptions persisted correctly.
- Job page returned HTTP 200.
- Python compile check passed.
- Quote totals remained unchanged.

### Current Uncommitted Work

- Revenue Adjustments route.
- Revenue Adjustments panel.
- Revenue Adjustments CSS.
- Stylesheet cache-version updates.

### Approved UI Polish

- Rename heading to `Additional Charges`.
- Show `Service Charge Added`.
- Show `Sourcing Fee Added`.
- Add temporary save-success confirmation.
- Add a Quote Summary revenue indicator after calculations are integrated.

### Next Task

Finish the UI polish, test it, update documentation, commit, and push.

### Last Stable Commit

`521375b` — PLG Alpha 8.3 documentation foundation

---

## 2026-08-09 — Alpha 8.3 Verification and Polish

### Current Release

PLG Alpha 8.3

### Purpose

Capture Service Charges and Sourcing Fees so billable work is not forgotten and the charges flow through Job → Quote → Invoice.

### Completed and Verified

- Created a full pre-change PPS safety checkpoint.
- Verified Revenue Adjustments route resolution.
- Verified Service Charge and Sourcing Fee save behavior using a temporary database.
- Verified Job, Quote, and Invoice database fields.
- Verified Quote calculation integration.
- Verified Invoice calculation integration.
- Verified Quote PDF charge support.
- Verified Invoice PDF charge support.
- Verified Additional Charges UI and CSS.
- Verified Quote Summary revenue and profitability sections.
- Renamed `Save Revenue Adjustments` to `Save Additional Charges`.
- Replaced the obsolete message saying Quote and Invoice totals were unchanged.
- New customer-facing message confirms Additional Charges are included in Quote and Invoice totals.

### Current Task

Update documentation, commit the verified Alpha 8.3 state, and push.

### Starting Commit

`26e77d1`

---

## 2026-08-09 — Alpha 8.3 Complete

### Release

PLG Alpha 8.3 — Revenue Adjustments / Additional Charges

### Final Status

COMPLETE

### Verified

- Service Charge and Sourcing Fee save correctly on the Job.
- Additional Charges are reflected in Job revenue and profit summaries.
- Charges flow into Quote calculations.
- Charges flow into Invoice calculations.
- Quote PDFs support Service Charge and Sourcing Fee.
- Invoice PDFs support Service Charge and Sourcing Fee.
- Customer-facing Additional Charges wording was corrected.
- Alpha 8.3 checkpoint was committed and pushed successfully.

### Protected Checkpoint

`9a1791a`

### Next Development Step

Review the PPS full-product-audit plan and begin the highest-priority unfinished milestone.

---

## 2026-08-09 — Alpha 19 Product Reconciliation Begins

### Active Phase

PPS Alpha 19 — Full Product Audit / Reconciliation

### Reason

The application version and installed roadmap modules are already Alpha 19 while the Current Sprint document was still tracking the completed Alpha 8.3 workstream.

### Objective

Audit what PPS actually implements today before choosing the next development milestone.

### Starting Checkpoint

`f80d4fd`

### Development Rule

Do not assume an audit item is missing merely because it appears on an older roadmap. Verify the current implementation first, then improve only genuine workflow gaps.

---

## 2026-08-09 — Alpha 19 Audit Remediation 1

### Finding

The modular migration source defined `_migration_0008_job_revenue_adjustments` twice.

The first duplicate also contained Smart Intake `customer_location_id` migration logic, but the second Python definition replaced it.

### Resolution

- Removed the duplicate Alpha 8.3 migration definition.
- Preserved Revenue Adjustments as migration `0008`.
- Added `0009_smart_intake_location_links`.
- Migration `0009` safely ensures `customer_location_id` exists on:
  - `customer_requests`
  - `machines`

### Verification

- Existing live database already contained both required columns.
- Migration `0009` passed an isolated in-memory test.
- Migration `0009` is idempotent.
- Full `run_migrations()` registration passed against a disposable database copy.
- SQLite integrity check passed.
- Migration source structure and Git diff check passed.

### Result

Migration history is now deterministic for both existing and fresh PPS databases.

No customer data repair was required.
