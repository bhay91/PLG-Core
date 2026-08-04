# PLG Commit 0004D — Streamlined Job-to-Quote Flow

This tests:

```text
Create Job
→ Parts Basket
→ Checkout Basket
→ Create Quote
```

## Changes

- Removes **Parts Requested** from New Job
- Removes **Internal Notes** from New Job
- New Job collects customer and equipment details only
- Newly created jobs open directly in the Parts Basket
- Keeps Vendor imports and manual Vendor line entry inside the Parts Basket
- Adds a functional **Checkout Basket** step
- After checkout, displays **Create Quote**
- Keeps existing Basket items, imports, pricing, and quote engine
- Automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004D.py
```

Restart PLG, create a test job, and confirm that it opens directly in the Parts Basket.
