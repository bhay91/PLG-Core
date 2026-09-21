# PPS Development Checkpoint — Research Import Connector Batch 2

## Runtime verification

On 2026-09-01, the temporary Firefox development extension successfully detected a
ChatGPT Research Import envelope and reconstructed the embedded PDF bytes and JSON
sidecar. The extension submitted the multipart package to:

`POST /api/extension/v1/research-import/packages`

PPS created proposal **22** as `DRAFT` for package
`CHATGPT-RESEARCH-SYNTHETIC-BATCH2-002`.

The PDF filename and SHA-256 matched exactly. The following candidate data survived
staging: synthetic customer and machine identity, PIN
`SYNTHETIC-BATCH2-001`, requested wording and quantity, and `$10.00 USD` estimated
inbound freight with status `ESTIMATED`.

The proposal remained unconfirmed:

- `created_job_id` was `NULL`.
- `created_request_id` was `NULL`.
- `confirmed_at` was `NULL`.
- No authoritative Job, customer, machine, Requested Need, quote, invoice,
  payment, supplier order, receiving, delivery, or accounting record was created.
- Operator **Confirm & Create** remained the authority boundary.

The first synthetic attempt was rejected because its test sidecar included the
unsupported field `machine.identifiers[].component_label`. This was a synthetic
package-construction error, not a Firefox transport defect. Removing that field
produced a schema-valid package.

## Safe disposal of an unconfirmed proposal

PPS already provides the governed Smart Intake review action:

`POST /requests/smart-intake/proposals/{proposal_id}/remove-from-inbox`

It requires the proposal lock version, only accepts a current `DRAFT`, changes the
proposal to `CANCELLED`, preserves its history, writes the existing
`SMART_INTAKE_CANCELLED` audit event, and redirects to `/requests`. Proposal 22 was
not cancelled or otherwise modified during this checkpoint.

## Verification status

- Batch 1 Research Import/backend regressions passed.
- Batch 2 connector/backend regressions passed.
- JavaScript syntax checks passed for `background.js` and `chatgpt_bridge.js`.
- `git diff --check` passed.
- Firefox bridge Playwright tests remain unavailable in this environment because
  Chromium exits with the known sandbox `SIGTRAP`; this is an environment limitation,
  not an application assertion failure.

No PPS database, production data, proposal 22, or deployment state was changed by
this documentation checkpoint.
