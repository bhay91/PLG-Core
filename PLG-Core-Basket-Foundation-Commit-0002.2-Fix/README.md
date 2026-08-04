# PLG Basket Foundation — Commit 0002.2 Fix

This fixes the installer validation for the newer FastAPI router behavior
present in your local environment.

The previous installer expected every registered application route to appear
directly in `app.routes` during the smoke test. Your FastAPI version stores an
included router as an internal `_IncludedRouter` object at that stage.

This version validates the Basket router directly, imports the live
application, applies the database migration, and preserves automatic rollback.

## Install

Stop PLG, extract this package, then run:

```bash
cd ~/Desktop/PLG-Core-Basket-Foundation-Commit-0002.2-Fix
python3 install_commit_0002.py
```

Restart PLG:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Verify:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`
