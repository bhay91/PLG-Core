# PLG Commit 0004G.1 — Stale Checkout Fix

Your Basket was marked `CHECKED_OUT` before the new checkout-to-quote bridge existed, so the page showed **Create Quote** even though no `job_parts` had been created.

This patch:

- Resets stale `CHECKED_OUT` baskets that have no job parts
- Shows **Create Quote** only after the Basket is truly `COMMITTED`
- Lets you click **Checkout Basket** again so the selected lines are copied into the quote system

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004G_1.py
```

Restart PLG, open the same job, click **Checkout Basket**, then **Create Quote**.
