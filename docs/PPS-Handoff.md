# Pinpoint Sourcing Co. — Development Handoff

## Project

Customer-facing product name: **Pinpoint Sourcing Co.**
Short brand: **PPS**

Internal repository/package name remains **PLG-Core** for now.

Repository:
https://github.com/bhay91/PLG-Core.git

Active branch:
`feature/job-workspace`

Local project:
`~/Desktop/PLG-Core`

Run locally:
`uvicorn plg_core.application:app --reload`

Primary database:
`data/plg_core.db`

## Current Git Checkpoint

Latest pushed commit:

`be176c1` — Add paid invoice custom invoice workflow

Previous important checkpoint:

`20daabb` — Lock PPS parts order and custom invoice requirements

Do not commit generated test PDFs or stray local database files unless intentionally needed.

## PPS Numbering Standard

- Invoice: `PPS-INV-0001`
- Quote: `PPS-Q-0001`
- Job: `PPS-J-0001`
- Opportunity: `PPS-OPP-0001`
- Customer: `PPS-C-0001`
- Machine: `PPS-M-0001`
- Supplier Order: `PPS-PO-0001`
- Receipt: `PPS-RCPT-0001`

Each category has its own numbering sequence.

## Locked PDF Design

The PPS Invoice PDF is the master visual standard.

Customer-facing documents should use:
- PPS blue / white / black branding
- PPS logo
- consistent header and document number placement
- customer/equipment information blocks
- standardized tables
- right-aligned totals
- fixed PPS footer treatment where appropriate

Locked document layouts currently include:
- Customer Invoice
- Internal Invoice
- Quote
- Parts Order Sheet

Parts Order Sheet is an internal purchasing document and does not need payment information or customer-facing legal/footer blocks.

## Invoice Workflow

Invoices support:
- UNPAID
- PARTIAL
- PAID
- VOID

Paid invoices automatically support purchasing controls.

Ordering is blocked until the latest non-void invoice is fully PAID.

The main invoice page displays paid invoice actions in a 2-column layout:

Left:
1. Open Invoice
2. Customer PDF
3. Internal PDF

Right:
1. Parts Order Sheet
2. Edit Custom Invoice
3. Custom Invoice

## Custom Invoice Feature

Implemented and working.

A Custom Invoice can only be created from a PAID invoice.

Behavior:
- Original paid invoice remains unchanged.
- Custom Invoice is stored separately.
- Number uses the original invoice number plus `C`.
  Example: `PPS-INV-0001C`
- Uses the locked PPS paid-invoice layout.
- Payment information is not shown.
- Can adjust values by percentage.
- Can enter an exact target total.
- Target-total mode proportionally adjusts line items.
- Can manually edit individual line-item prices.
- Manual edits recalculate totals.
- Can save, reopen, edit again, and re-save.
- No reset button.
- Saving returns to the main Invoices page.
- Custom Invoice PDF regenerates automatically after every save.
- Original invoice, payment record, supplier costs, profit, ledger, and accounting remain unchanged.

Database migration:
`0010_custom_invoices`

Tables:
- `custom_invoices`
- `custom_invoice_items`

Primary files:
- `legacy_app.py`
- `plg_core/database/migrations.py`
- `plg_core/documents/invoice_pdf.py`
- `templates/custom_invoice.html`
- `templates/invoices.html`
- `templates/invoice_documents.html`
- `static/app.css`

## Opportunities

Opportunity system is implemented with:
- opportunities
- opportunity machines
- opportunity research/evidence
- follow-up date
- ChatGPT research imports
- full ChatGPT opportunity package import
- Opportunity → Job conversion
- customer and converted Job display

Opportunity research/evidence should remain permanently stored in PPS.

Every customer sourcing request should become an Opportunity until converted, closed, or archived.

## Development Roadmap

Read:
`docs/Roadmap.md`

Current development phase:
**Alpha 9 — Opportunities & ChatGPT Research**

Remaining Alpha 9 work includes:
- better Opportunities dashboard
- customer on opportunity list
- machine count
- research count
- converted Job in list
- status filters
- follow-up filtering
- edit Opportunity
- change/assign customer
- attach existing registered machine
- VIN/PIN/serial duplicate-machine protection
- better multi-machine handling
- Job link back to originating Opportunity
- conversion timeline event
- converted Opportunity behavior

Future phases:

- Alpha 10 — Parts Intelligence & Verification
- Alpha 11 — Pricing & Business Intelligence
- Alpha 12 — Quotes & Sales Conversion
- Alpha 13 — Invoices & Payments
- Alpha 14 — Purchasing & Supplier Orders
- Alpha 15 — Receiving & Delivery
- Alpha 16 — Customers, Machines & Search
- Alpha 17 — Dashboard & Follow-Up Center
- Alpha 18 — Accounting, Documents & Audit
- Alpha 19 — API & Production Hardening
- Beta
- Release Candidate
- Stable

## Product Architecture

ChatGPT is the intelligence/research layer.

PPS is the permanent operational source of truth.

Research, evidence, part identification, customer records, machine records, opportunities, jobs, quotes, invoices, payments, purchasing, receiving, delivery, and accounting history should persist in PPS rather than disappear into chat history.

Preferred workflow:

Customer Request
→ Opportunity
→ ChatGPT Research
→ Evidence
→ Machine
→ Verified Parts
→ Job
→ Quote
→ Invoice
→ Payment
→ Supplier Order
→ Receiving
→ Delivery
→ Accounting / History

## Starting a New Chat

Tell ChatGPT:

"Let's build PPS. We are continuing the existing PLG-Core project on branch feature/job-workspace. Read docs/PPS-Handoff.md and docs/Roadmap.md first, then continue development from the current state. Give me one small terminal command at a time."

