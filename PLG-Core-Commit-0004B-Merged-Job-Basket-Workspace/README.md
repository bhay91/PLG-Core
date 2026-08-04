# PLG Commit 0004B — Merged Job & Basket Workspace

This merges the Job Overview and Parts Basket into one daily workspace.

## Changes

- Clicking a recent job opens the unified Job workspace
- Clicking a job in the Jobs Register opens the unified Job workspace
- Customer, machine, VIN, status, and Basket are shown together
- Status update and Edit Job Information remain available
- The normal workflow no longer requires a separate **Open Parts Basket** click
- Overview and Basket are represented as one active workspace tab
- Timeline, Documents, and Notes remain planned secondary tabs
- Committing Basket items returns to the same unified workspace
- Existing legacy Job route remains available internally
- Automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004B.py
```

Restart:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Click a job from the Dashboard or Jobs page.
