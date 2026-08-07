# PLG Roadmap

## Alpha 8 — Core Revenue & Operations Foundation — COMPLETED / MOSTLY COMPLETED

- Revenue protection.
- Revenue adjustments.
- Revenue checklist.
- Service charge workflow.
- Smart sourcing fee foundation.
- Quote and invoice integration foundation.
- Dashboard activity foundation.
- Today's Priorities foundation.
- Suggested Next Action foundation.
- Workflow reminders foundation.
- Search/navigation foundation.
- Payment workflow.
- Payment-before-ordering protection.
- Job workflow foundation.

## Alpha 9 — Opportunities & ChatGPT Research — CURRENT

Completed through Alpha 9.10:

- Opportunity database.
- Opportunity creation and list.
- Opportunity detail page.
- Multiple machines per Opportunity.
- Research/evidence records.
- Clickable source links.
- Follow-up management.
- ChatGPT single research import.
- ChatGPT batch research import.
- ChatGPT Opportunity import.
- ChatGPT Opportunity + machine import.
- Full ChatGPT Opportunity package import.
- Customer-linked Opportunities.
- Opportunity to Job conversion.
- Machine registry creation/linking.
- Duplicate Job conversion protection.
- Research to Job basket transfer.
- Source URL normalization.
- Customer shown on Opportunity.
- Converted Job shown and linkable.

Remaining Alpha 9 work:

- Better Opportunities dashboard.
- Customer shown on Opportunity list.
- Machine count.
- Research count.
- Converted Job shown in list.
- Status filters.
- Follow-up filtering.
- Edit Opportunity.
- Change/assign customer.
- Attach existing registered machine.
- VIN/PIN/serial duplicate machine protection.
- Better handling of multi-machine Opportunities.
- Link from Job back to originating Opportunity.
- Record conversion in Job timeline.
- Make converted Opportunities behave appropriately after conversion.

## Alpha 10 — Parts Intelligence & Verification

- Improve sourcing basket.
- Requested part vs candidate part distinction.
- OEM candidate.
- Aftermarket candidate.
- Supplier alternatives.
- Multiple supplier offers per part.
- OEM part number.
- Alternate/superseded numbers.
- Supplier part number.
- Supplier price.
- Shipping.
- Availability.
- Lead time.
- Confidence.
- Evidence/source URLs.
- Verify candidate.
- Reject candidate.
- Select candidate.
- Part verification status.
- VIN/PIN/serial compatibility verification.
- Manual override with audit note.
- CAT SIS imports.
- 7zap imports.
- Generic supplier research imports.
- One standard Verify Parts workflow.

## Alpha 11 — Pricing & Business Intelligence

- Pricing Assistant.
- Smart Parts Margin recommendations.
- Sourcing Fee recommendations.
- PLG markup rules.
- Customer price rounding.
- Revenue breakdown.
- Cost breakdown.
- Gross profit.
- Profit analysis.
- Customer analytics.
- Supplier analytics.
- Job profitability.
- Margin warnings.
- Revenue protection checks.

## Alpha 12 — Quotes & Sales Conversion

- Generate quote directly from selected basket items.
- Automatic markup.
- Sourcing fee.
- Service charge.
- Shipping.
- Quote number.
- Quote PDF.
- Customer-facing prices only.
- Internal cost/profit view.
- Quote revisions.
- Quote history.
- Accepted / declined status.
- Convert accepted quote to invoice.
- Preserve all Job/Opportunity relationships.

## Alpha 13 — Invoices & Payments

- Connect locked PLG invoice design to Job workflow.
- Customer Invoice PDF.
- Internal Accounting PDF.
- Automatic invoice numbering.
- Customer/machine/VIN details.
- Subtotal.
- Sourcing fee.
- Service charge.
- Total.
- Payment information.
- Partial payment.
- Paid status.
- Remaining balance.
- Payment history.
- Payment method/reference.
- Paid PDF.
- Job status integration.
- Payment to READY_TO_ORDER.
- Keep payment-before-ordering protection.

## Alpha 14 — Purchasing & Supplier Orders

- Convert paid selected items into supplier orders.
- Supplier grouping.
- Order date.
- Supplier order/reference number.
- Actual purchase cost.
- Shipping cost.
- Tracking number.
- Backordered items.
- Multiple supplier orders per Job.
- Partial ordering.
- Prevent duplicate purchasing.
- Ordered vs still-needed visibility.
- Supplier notes.
- Order timeline events.

## Alpha 15 — Receiving & Delivery

- Receive individual parts.
- Partial receiving.
- Quantity received.
- Wrong/damaged item.
- Receiving notes.
- Received date.
- Tracking.
- Delivery/pickup.
- Customer shipment.
- Delivery date.
- Final Job completion.
- Automatically determine when all parts are received/delivered.
- Complete Job history.

## Alpha 16 — Customers, Machines & Search

### Customer profile

- Contact information.
- Locations.
- Machines.
- Opportunities.
- Jobs.
- Quotes.
- Invoices.
- Payment history.
- Previously supplied parts.

### Machine profile

- Manufacturer.
- Model.
- Year.
- VIN/PIN/serial.
- Engine.
- Engine serial.
- Transmission.
- Components.
- Previous Jobs.
- Previous research.
- Verified parts history.

### Global search

- Customer.
- Customer number.
- Machine.
- VIN/PIN/serial.
- Engine serial.
- Opportunity.
- Job.
- Quote.
- Invoice.
- OEM part number.
- Alternate number.
- Supplier number.
- Command palette.

## Alpha 17 — Dashboard & Follow-Up Center

- Opportunities needing follow-up.
- Open sourcing work.
- Parts needing verification.
- Quotes awaiting customer.
- Invoices awaiting payment.
- Jobs ready to order.
- Supplier orders pending.
- Parts in transit.
- Parts ready for customer.
- Overdue actions.
- Suggested next action.
- Job Readiness Score.
- Workflow reminders.

## Alpha 18 — Accounting, Documents & Audit

- Revenue ledger.
- Parts cost.
- Shipping cost.
- Supplier charges.
- Sourcing/service fees.
- Gross profit.
- Outstanding receivables.
- Customer totals.
- Supplier totals.
- Job profitability.
- Quote documents.
- Invoice documents.
- Receipt documents.
- Purchase/order documents.
- Document versions.
- Audit trail.
- ChatGPT-created records identified.
- Manual changes recorded.
- Verification changes.
- Price changes.
- Payment changes.
- Order changes.

## Alpha 19 — API & Production Hardening

- Pydantic schemas for ChatGPT APIs.
- Proper validation.
- Customer validation.
- Machine validation.
- Database constraints.
- Duplicate protections.
- Transaction cleanup.
- Better errors.
- Refactor temporary Alpha code.
- Automated workflow tests.
- Authentication.
- User permissions.
- API authentication.
- Secrets/environment configuration.
- Production-ready migrations.
- File/evidence storage architecture.

## Beta

- Feature completeness.
- Full end-to-end workflow testing.
- Realistic sample Jobs.
- Data-reset procedure for production.
- Remove test Opportunities/Jobs/customers.
- Performance review.
- Security review.
- Documentation review.
- Mobile/browser testing.
- Accounting validation.
- PDF validation.
- ChatGPT integration testing.

## Release Candidate

- Bug fixes only.
- No new major features.
- Production migration testing.
- PostgreSQL migration testing.
- Backup/restore verification.
- Authentication/security verification.
- Final PDF verification.
- Final accounting verification.
- Production deployment rehearsal.

## Stable

- Production-ready PLG release.
- PostgreSQL.
- Backups.
- Secure authentication.
- File/evidence storage.
- HTTPS/domain.
- Monitoring.
- ChatGPT and PLG operational integration.

## Final PLG Workflow

Customer request
→ Opportunity
→ ChatGPT research
→ evidence
→ machine
→ verified parts
→ Job
→ quote
→ invoice
→ payment
→ supplier order
→ receiving
→ delivery
→ accounting/history
