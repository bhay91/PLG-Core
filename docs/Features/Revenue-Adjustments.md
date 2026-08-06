# Revenue Adjustments

## Status

In Progress

## Priority

Critical

## Release Target

PLG Alpha 8.3

## Problem

Additional billable work and sourcing expertise may be forgotten during a fast-moving quote workflow.

## Business Goal

Protect revenue while keeping quote creation fast and under the user's control.

## Revenue Types

### Service Charge

Compensation for work personally performed.

Examples:

- inspections;
- delivery to a shipping agent;
- customer or supplier visits;
- programming;
- measurements;
- consulting;
- special coordination.

Rules:

- stored on the Job;
- optional;
- $0 means not used;
- if positive, minimum $150;
- no maximum;
- manually selected and entered by the user;
- PLG does not recommend the amount.

### Sourcing Fee

Compensation for sourcing expertise.

Examples:

- multiple suppliers;
- difficult OEM research;
- cross-referencing;
- verification;
- international sourcing.

Rules:

- stored on the Job;
- optional;
- $0 means not used;
- nonnegative;
- PLG may recommend an amount later;
- user can edit or remove it.

## Approved Workflow

1. User researches and selects parts.
2. Revenue Adjustments panel appears before Quote Summary.
3. User may save a Service Charge, Sourcing Fee, or both.
4. When quote generation is attempted and no charge exists, PLG reminds the user.
5. The user may:
   - return to Revenue Adjustments;
   - continue without adding charges.
6. If a charge already exists, PLG does not ask about that charge again.
7. Charges later flow into Quotes, Invoices, PDFs, and Business Intelligence.

## Database

The following fields exist on `jobs`:

- `service_charge REAL NOT NULL DEFAULT 0`
- `service_charge_description TEXT NOT NULL DEFAULT ''`
- `sourcing_fee REAL NOT NULL DEFAULT 0`
- `sourcing_fee_description TEXT NOT NULL DEFAULT ''`

Migration:

`0008_job_revenue_adjustments`

## Current Milestone

Build a Revenue Adjustments panel in the Job Command Center.

This milestone must only:

- display values;
- edit values;
- validate values;
- save values;
- reload values.

It must not yet change:

- Quote totals;
- Invoice totals;
- PDFs;
- Customer ledger;
- Business Intelligence totals.

## Validation

### Service Charge

Allowed:

- `0`
- any amount greater than or equal to `150`

Rejected:

- negative values;
- positive values below `150`;
- invalid numbers.

### Sourcing Fee

Allowed:

- `0`
- any nonnegative valid number.

Rejected:

- negative values;
- invalid numbers.

Descriptions are optional.

## Definition of Done

- [x] Business model approved.
- [x] Job-level database fields created.
- [x] Migration tested.
- [x] Migration committed and pushed.
- [ ] Revenue Adjustments panel displayed.
- [ ] Values save correctly.
- [ ] Values reload correctly.
- [ ] Service Charge validation works.
- [ ] Sourcing Fee validation works.
- [ ] Quote reminder works.
- [ ] Quote calculations include charges.
- [ ] Invoice calculations include charges.
- [ ] Customer and internal PDFs include charges.
- [ ] Business Intelligence reports charges separately.
- [ ] End-to-end tests pass.
- [ ] Release history updated.

## Approved UI Polish

- Section heading displays `Additional Charges`.
- Active Service Charge badge displays `🟢 Service Charge Added`.
- Active Sourcing Fee badge displays `🔵 Sourcing Fee Added`.
- A temporary success confirmation appears after saving.
- A small Quote Summary revenue indicator will be added when charge calculations are integrated.
