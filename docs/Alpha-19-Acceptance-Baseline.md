# PPS Alpha 19 — Acceptance Baseline

## Release identity

- Product: Pinpoint Sourcing (PPS)
- Application version: `1.0.0-alpha.19`
- Phase: `alpha-19` / PPS Alpha 19
- Deployment status: **NOT DEPLOYED**

## Acceptance status

Alpha 19 establishes the protected application checkpoint for PPS workflow, sourcing, fulfillment, document, and internal integration capabilities. Further deployment or release activity requires explicit authorization and a separate deployment review.

## Accepted capabilities

### Smart Intake and sourcing

- Smart Intake captures proposed customer, asset, requested-need, attachment, and research context for operator review.
- Imported or AI-assisted content remains proposed evidence until an operator confirms it.
- Customer, asset, identifier, and requested-need associations retain explicit review boundaries.
- Research sources and captured evidence retain provenance and do not become authoritative merely because they were imported.
- Supplier and catalog capture supports structured part, price, availability, logistics, and source context.

### Commercial workflow

- PPS supports the Job, Quote, Invoice, Payment, Supplier Order, Receiving, Delivery, and Accounting workflow.
- Quotes and invoices preserve authoritative commercial values and historical revisions.
- Presentation-only custom invoice adjustments do not rewrite underlying authoritative invoice records.
- Customer-facing and internal document audiences remain separated.

### Purchasing and fulfillment

- Supplier orders retain ordered, received, delivered, remaining, and available-to-deliver quantities.
- Partial receiving and partial delivery are supported.
- Receiving and delivery operations enforce quantity limits and preserve attribution, audit, and idempotency behavior.
- Completion is derived from fulfillment progress rather than presentation state alone.
- Actual supplier-cost confirmation is separated from booked and placed cost snapshots.

### Documents and integrity

- Quote, invoice, supplier-order, receiving, and delivery documents use manifest-backed resolution where applicable.
- Document versions and audiences remain distinct, with current and historical records preserved.
- Manifest resolution fails closed for missing, mismatched, wrong-family, or invalid document references.
- Customer documents exclude internal supplier-cost, markup, and profit information.
- Generated documents and customer runtime files are not part of this source checkpoint.

### Internal integrations and authority boundaries

- Firefox and MCP integrations use explicit authentication scopes.
- Integration submissions create reviewable proposals or invoke narrowly authorized actions; they do not bypass PPS authority boundaries.
- Smart Intake confirmation remains an operator-controlled browser workflow.
- Job-action endpoints retain authorization, validation, audit, and idempotency requirements.
- Database identifiers are not substitutes for PPS business-number validation at integration boundaries.

## Verification status

- **605/605 non-browser tests passed** together in one pytest process.
- **123 subtests passed** in that same non-browser checkpoint run.
- The Firefox ChatGPT bridge previously passed **19/19 tests** with Chromium available.
- Chromium-dependent tests cannot currently all be rerun inside the Codex sandbox because browser launch permission and sandbox restrictions interfere.
- Python `compileall` passed for `plg_core` and `legacy_app.py`.
- `git diff --cached --check` passed before this documentation correction.
- The authoritative live database remained unchanged throughout verification and was not used directly for test writes.
- Test writes used disposable database, document, and upload roots.
- PPS remained stopped during checkpoint verification.

## Release boundaries

- This checkpoint does not authorize deployment, migration of operational data, or changes to customer documents.
- Runtime databases, documents, uploads, generated packages, backups, and secrets remain excluded from the source checkpoint.
- Production configuration and secrets must be supplied separately through an approved deployment process.
- PPS remains an internal application and has not been deployed by this checkpoint.

## Known verification limitation

The complete non-browser suite is green in a single process. Browser-dependent coverage has a previously verified Firefox bridge result, but the complete Chromium-dependent set requires an execution environment where Chromium can launch successfully.
