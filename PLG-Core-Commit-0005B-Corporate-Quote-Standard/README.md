# PLG Commit 0005B - Corporate Quote Standard

This upgrades the real quote PDF engine to match the approved PartsLink Global corporate template.

## Customer Quote
- Logo and full business contact information
- Quote number, date, valid-until date, and status
- Customer and equipment information
- Professional parts table
- Payment information
- Subtotal, shipping, sourcing fee, service charge, and total
- Parts Identification and Warranty sections
- Corporate document version and page number

## Internal Quote
- Matching corporate design
- Vendor, part number, supplier cost, customer selling price, and profit
- Supplier total, customer total, and net profit
- Internal-use confidentiality notice

## Install
Stop PLG and run:

```bash
python3 install_commit_0005B.py
```

Restart PLG, then open any existing quote PDF. Existing quotes are regenerated automatically.
