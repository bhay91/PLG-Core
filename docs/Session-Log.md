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
