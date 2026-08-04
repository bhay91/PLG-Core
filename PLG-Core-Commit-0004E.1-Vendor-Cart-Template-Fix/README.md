# PLG Commit 0004E.1 — Vendor Cart Template Fix

The imported Vendor Cart was saved, but Jinja interpreted:

```jinja2
vendor_cart.items
```

as Python's dictionary `.items()` method.

This patch changes Vendor Cart template access to dictionary-key syntax.

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004E_1.py
```

Restart PLG and reopen the same job. The already imported Vendor Cart should appear.
