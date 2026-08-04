# PLG Commit 0004C — Vendor Carts Workflow

This tests the new sourcing model:

1. Import or manually create **Vendor Carts**
2. Keep every Vendor's line items separate
3. Add chosen line items to the **Parts Basket**
4. Apply PLG pricing only to the final Basket

## Included

- Vendor Carts grouped by Vendor
- Imports no longer automatically enter the final Parts Basket
- Manual Vendor line entry
- Add individual line items to Parts Basket
- Remove items from Parts Basket without deleting Vendor lines
- Final Basket table and totals
- Existing import endpoints and commit workflow preserved
- Automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004C.py
```

Restart PLG and open a job.
