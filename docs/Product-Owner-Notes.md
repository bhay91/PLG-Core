# PLG Product Owner Notes

## PON-001 — PLG Must Remember

Observation:

When work moves quickly across customers, suppliers, quotes, payments, and shipping coordination, important details can be forgotten.

Decision:

PLG should act as a second set of eyes and remember what Brandon should not have to remember.

Business Benefit:

Reduce mistakes, prevent lost revenue, and make daily work easier.

## PON-002 — Service Charges

Observation:

Additional work such as inspection, delivery to a shipping agent, programming, measurements, and coordination may need to be billed separately.

Decision:

Service Charge belongs to the Job and remains manually controlled.

Business Benefit:

Prevent additional work from being performed without compensation.

## PON-003 — Sourcing Expertise

Observation:

Finding, verifying, and coordinating parts can have value separate from the parts themselves.

Decision:

Sourcing Fee is a separate job-level revenue stream.

Business Benefit:

Measure and bill for sourcing expertise accurately.

## PON-004 — Documentation as Source of Truth

Observation:

Long conversations and memory are not reliable enough to preserve all development decisions.

Decision:

The `docs/` directory is the authoritative source of truth for PLG development.

Business Benefit:

Future development can resume accurately without rediscovering past decisions.

## Pinpoint Sourcing Co. Rebrand Decision

- Customer-facing business name: Pinpoint Sourcing Co.
- Short brand mark: PPS.
- Preferred brand colors: blue, white, and black.
- Preferred logo direction: clean target/location-pin concept representing precision, sourcing, and direction.
- PLG-Core remains the internal software/repository name for now.
- Existing PLG record numbers should not be changed automatically.
- A later implementation decision will determine whether new customer, machine, opportunity, job, quote, and invoice numbers switch from PLG prefixes to PPS prefixes.
- Rebranding should be handled as a controlled customer-facing change so existing operational history is preserved.

## PPS Master PDF Standard

The final locked PPS invoice layout is the master visual standard for all future customer-facing and internal PDF documents.

Use the same:
- PPS logo and company/contact header
- Blue, white, and black brand styling
- Right-side document title and document number block
- Customer and equipment information styling
- Table typography, borders, spacing, and alignment
- Right-aligned totals area
- Fixed bottom information section:
  - Parts Identification
  - Pinpoint Sourcing Co. thank-you block
  - Warranty Information
- Thin page footer with document number and page count

Each document keeps its own content and business logic, but should visually inherit this PPS master PDF shell.

Examples:
- Invoice: PPS-INV-0001
- Quote: PPS-Q-0001
- Job-related PDF: PPS-J-0001 where appropriate
- Supplier/Purchase Order: PPS-PO-0001
- Receipt: PPS-RCPT-0001

Locked reference invoice layout commit: 097e8ee

## Custom Invoice

A Custom Invoice may only be created from an invoice that is already marked PAID.

- The original paid invoice remains unchanged.
- The Custom Invoice is a separate saved version linked to the original invoice.
- It uses the same locked PPS invoice layout.
- It may use a C suffix on the original invoice number, e.g. PPS-INV-0001C.
- Payment information is not shown.
- User may adjust values by percentage.
- User may enter an exact target total and PPS proportionally recalculates item values.
- User may manually edit individual line-item prices.
- Manual line-item edits automatically recalculate line totals and the invoice total.
- The Custom Invoice can be saved, reopened, edited again, and re-saved.
- No reset-to-original-values control is required.
- Changes to the Custom Invoice do not modify the original invoice, payment record, supplier costs, profit, ledger, or accounting data.
## Custom Invoice

A Custom Invoice may only be created from an invoice that is already marked PAID.

- The original paid invoice remains unchanged.
- The Custom Invoice is a separate saved version linked to the original invoice.
- It uses the same locked PPS invoice layout.
- It uses a C suffix on the original invoice number, e.g. PPS-INV-0001C.
- Payment information is not shown.
- User may adjust values by percentage.
- User may enter an exact target total and PPS proportionally recalculates item values.
- User may manually edit individual line-item prices.
- Manual line-item edits automatically recalculate line totals and the invoice total.
- The Custom Invoice can be saved, reopened, edited again, and re-saved.
- No reset-to-original-values control is required.
- Changes to the Custom Invoice do not modify the original invoice, payment record, supplier costs, profit, ledger, or accounting data.

