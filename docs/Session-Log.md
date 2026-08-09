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

---

## 2026-08-09 — Alpha 19 Audit Remediation 2

### Finding

Customer Request and Smart Intake Job creation were bypassing the modern Job Basket.

Requested parts were being inserted directly into legacy `job_parts`, while the Job Command Center works from `baskets` and `basket_items`.

This could create a Job whose requested parts existed in the database but were not visible in its active sourcing workspace.

### Architecture Confirmed

The intended parts flow is:

Customer Request / Smart Intake / Opportunity
→ Job Basket
→ research and sourcing
→ Basket commit
→ `job_parts`

`job_parts` remains a downstream compatibility/document workflow store rather than the primary working parts store.

### Resolution

- Added `add_item_with_connection()` to the Basket service.
- Preserved the existing public `add_item()` behavior.
- Customer Request → Job now adds requested parts to the Job Basket.
- Smart Intake → Job now adds requested parts to the Job Basket.
- Initial requested parts are no longer written directly into `job_parts`.
- This prevents duplicate `job_parts` rows when the Basket is later committed.

### Verification

- Connection-aware Basket helper passed isolated testing.
- Existing `add_item()` behavior passed regression testing.
- No existing Request-created Jobs required data backfill.
- Both affected intake functions were confirmed and repaired.
- Customer Request → Job → Basket passed end-to-end against a disposable copy of the real PPS database.
- Requested parts appeared in the Job Basket.
- No premature legacy `job_parts` records were created.
- Source syntax and Git diff checks passed.

### ChatGPT Workflow Requirement

ChatGPT remains a supported natural-language front door into PPS.

The user must continue to be able to enter reminders, follow-ups, customer requests, sourcing tasks, and other things they do not want to forget through ChatGPT instead of having to manually open PPS first.

Future PPS workflow changes must preserve this capability.

### Result

All current Request-based Job creation paths now feed the same working Basket architecture used by the Job Command Center.

---

## 2026-08-09 — Alpha 19 Audit Remediation 3

### Finding

Customer Requests and Opportunities were both active PPS workflows, but there was no persistent relationship between them.

A Request could create a Job directly while an Opportunity could independently create another Job, leaving PPS with competing intake paths and no reliable way to know which Opportunity originated from which Request.

### Resolution

Added an explicit Customer Request → Opportunity relationship using `opportunities.customer_request_id`.

The supported workflow is now:

Customer Request
→ Opportunity
→ sourcing / research / evidence / follow-up
→ Job
→ Job Basket

For straightforward requests that do not require Opportunity work, direct Request → Job remains available.

Once a Request has an Opportunity, that Opportunity becomes the authoritative path and the direct Job action will not create a competing Job.

### Request → Opportunity

Creating an Opportunity from a Customer Request now carries forward:

- customer
- request message
- reminder date as Opportunity follow-up
- linked Registry item or entered equipment information
- requested parts as Opportunity research candidates
- permanent originating Customer Request link

Repeated creation reuses the existing linked Opportunity instead of creating duplicates.

### Opportunity → Job

When a linked Opportunity becomes a Job:

- the Opportunity records the converted Job
- the originating Customer Request records the same Job
- the Customer Request becomes `COMPLETED`
- Opportunity research is transferred into the Job Basket
- no initial duplicate `job_parts` records are created

### User Interface

The Customer Request workflow now displays:

1. Customer
2. Registry
3. Opportunity
4. Job

The Request screen can create or open its Opportunity.

For simple work, `Create Job Directly` remains available when no Opportunity exists.

### Verification

Passed:

- Request → Opportunity end-to-end test
- duplicate Opportunity prevention
- Request → Opportunity → Job → Basket end-to-end test
- Customer Request completion synchronization
- Opportunity and Request pointing to the same Job
- Basket research transfer
- no premature `job_parts`
- repeated Opportunity conversion does not duplicate Jobs
- direct Job route cannot bypass an existing Opportunity
- converted Opportunity reuses its existing Job
- Request Jinja template loading
- final Python syntax and Git diff checks

### Result

PPS now has one coherent intake architecture instead of disconnected Request and Opportunity workflows.

---

## 2026-08-09 — Alpha 19 Audit Remediation 4

### Finding

Opportunities were a real operational PPS workflow and were now connected to Customer Requests and Jobs, but they were still absent from the main navigation.

That made an important sourcing and research stage discoverable only by entering it from another record.

### Resolution

Added Opportunities to the PPS Daily Work navigation between Customer Requests and Jobs.

The visible workflow order is now:

Follow-Up Center
→ Customer Requests
→ Opportunities
→ Jobs
→ Quotes
→ Invoices
→ Purchasing

Opportunity create, list, and detail pages now set the Opportunities navigation item as active.

### Verification

Passed:

- Opportunities route exists
- Opportunity detail route exists
- sidebar previously had no Opportunity link
- one Opportunities link added
- navigation order is Customer Requests → Opportunities → Jobs
- Opportunity create/list/detail pages all set the correct active navigation state
- Python syntax check
- Git diff whitespace check

### Result

Opportunities are now a first-class, directly accessible part of the PPS operating workflow rather than a hidden intermediate feature.

---

## 2026-08-09 — Alpha 19 Audit Remediation 5

### Finding

Smart Intake was still operating as a separate shortcut.

It created or linked the customer, location, Registry item, and Customer Request, but then immediately created a Job and Basket instead of using the newly established Customer Request → Opportunity workflow.

This meant natural-language intake could bypass the sourcing, research, evidence, and follow-up stage.

### Resolution

Smart Intake now uses the same PPS workflow as normal Customer Requests:

Smart Intake
→ Customer / Registry
→ Customer Request
→ Opportunity
→ sourcing / research / evidence / follow-up
→ Job
→ Basket

Smart Intake no longer creates a Job directly.

After creating the Customer Request, it routes that Request through the existing Request → Opportunity bridge.

### Enter Information Once

When Smart Intake already links a Registry machine to the Customer Request:

- the Opportunity retains that same Registry machine
- converting the Opportunity automatically carries that machine into the Job
- the user does not need to select the same machine again
- an explicitly selected Opportunity machine can still override the automatic choice

The Opportunity conversion screen now explains this behavior with:

`Use linked Request machine automatically`

### Requested Parts

Requested parts entered through Smart Intake become Opportunity research records with source type `REQUEST`.

When the Opportunity is converted, those records become Job Basket candidates.

They are not written prematurely to `job_parts`.

### Verification

Passed:

- Smart Intake creates or links Customer and Registry records
- Smart Intake creates the Customer Request
- Smart Intake no longer creates a Job immediately
- Smart Intake redirects to a linked Opportunity
- requested parts become Opportunity research records
- Opportunity remains linked to its originating Request
- Request Registry machine carries automatically into the Job
- Opportunity conversion updates the Request to `COMPLETED`
- Request and Opportunity reference the same Job
- requested parts transfer into the Basket
- no premature `job_parts` records are created
- repeated Opportunity conversion reuses the existing Job
- Opportunity template loads successfully
- Python syntax checks
- Git diff whitespace checks

### Result

Smart Intake is now a natural-language front door into the same PPS operating workflow rather than a competing Job-creation path.

---

## 2026-08-09 — Alpha 19 Audit Remediation 6

### Finding

Customer Request creation still used the legacy `PLG-Rxxxxx` numbering prefix in both normal Request entry and Smart Intake.

This conflicted with the PPS product identity.

### Resolution

Future Customer Requests now use:

`PPS-Rxxxxx`

This applies to:

- normal Customer Request creation
- Smart Intake Request creation

Existing historical `PLG-Rxxxxx` Request numbers are preserved unchanged.

Historical record identifiers are not rewritten during rebranding.

### Verification

Passed:

- both Request creation paths previously used `PLG-R`
- no unexpected third Request numbering scheme was found
- normal Request creation now produces `PPS-R`
- Smart Intake now produces `PPS-R`
- existing `PLG-R` records remain unchanged
- Python syntax check
- Git diff whitespace check

### Result

New Customer Request records now use PPS branding while historical Request identity remains stable.

---

## 2026-08-09 — Alpha 19 Audit Remediation 7

### Finding

Active PPS templates still contained several visible legacy `PLG` references after the product rebrand.

These appeared in:

- dashboard fallback labels
- Quotes fallback labels
- Invoices fallback labels
- Registry helper text
- Customer Request helper text
- Smart Intake instructions
- Job Command Center labels and messages

The PDF rendering code also contains internal ReportLab style names such as `PLGBrand` and `PLGTagline`.

### Resolution

Visible application wording was updated from `PLG` to `PPS`.

Internal implementation identifiers that are not rendered to users were intentionally left unchanged.

Historical `PLG-Rxxxxx` Request identifiers also remain valid and unchanged.

### Verification

Passed:

- all seven edited templates load in the real Jinja environment
- dashboard fallback branding uses PPS
- Quotes fallback branding uses PPS
- Invoices fallback branding uses PPS
- Registry helper text uses PPS
- Request helper text uses PPS
- Smart Intake instructions use PPS
- Job Command Center messages use PPS
- no remaining non-historical `PLG` references exist in active HTML templates
- Git diff whitespace check

### Result

The active PPS interface now presents consistent PPS branding without risky or unnecessary internal renaming.

---

## 2026-08-09 — Alpha 19 Audit Remediation 8

### Finding

The PPS Invoice PDF had two narrow layout columns that could wrap important labels:

- the top paid status value could wrap `PAID IN FULL`
- the equipment information label could wrap `VIN / PIN / Serial:`

The bottom totals/payment table was inspected separately and already had sufficient width, so it did not require modification.

### Resolution

Adjusted only the affected column widths in `plg_core/documents/invoice_pdf.py`.

The overall section widths remain unchanged.

No pricing, payment, accounting, invoice-status, or totals logic was modified.

### Verification

Passed:

- Python compilation
- Git diff whitespace check
- ReportLab single-line measurement for `PAID IN FULL`
- ReportLab single-line measurement for `VIN / PIN / Serial:`
- disposable customer Invoice PDF generation
- real Poppler PDF rendering
- rendered PDF text-line verification
- both customer-PDF `PAID IN FULL` occurrences remain single-line
- equipment identifier label remains single-line

### Result

The locked PPS Invoice PDF design is preserved while the two identified wrapping defects are corrected.

---

## 2026-08-09 — Alpha 19 Audit Remediation 9

### Finding

Customer-facing Quote and Invoice PDFs displayed `Not Provided` for missing optional information such as address, phone, email, equipment details, and VIN/PIN/Serial.

This made otherwise complete customer documents look unfinished.

Internal documents still benefit from explicitly showing missing operational information.

### Resolution

Updated the existing Quote and Invoice information-box rendering so:

- customer PDFs omit empty optional rows
- internal PDFs continue showing `Not Provided`
- the existing PPS information-box layout is preserved
- no pricing, totals, accounting, or workflow logic is changed

### Verification

Passed:

- Python compilation
- Git diff whitespace checks
- disposable customer Quote rendering
- disposable internal Quote rendering
- disposable customer Invoice rendering
- disposable internal Invoice rendering
- customer PDFs contain zero `Not Provided` placeholders
- empty optional customer-data rows are omitted
- required customer identity still renders
- internal PDFs continue displaying missing-data indicators
- PPS business contact information remains unaffected

### Result

Customer Quote and Invoice PDFs now present missing optional information cleanly while internal PPS documents continue identifying incomplete operational data.
