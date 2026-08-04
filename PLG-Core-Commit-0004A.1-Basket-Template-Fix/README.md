# PLG Commit 0004A.1 — Basket Template Fix

The Basket page failed because Jinja interpreted `basket.items` as Python's dictionary `.items()` method.

This patch changes the template to use:

```jinja2
basket["items"]
```

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004A_1.py
```

Restart PLG and reopen the Parts Basket.
