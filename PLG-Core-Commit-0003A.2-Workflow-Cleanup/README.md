# PLG Commit 0003A.2 — Workflow Cleanup

This completes the approved Corporate Shell cleanup.

## Changes

- Corrects every webpage occurrence to **Parts Sourcing Control Center**
- Removes the old **ADD MORE PARTS** panel from Job Details
- Removes **Requested description**, **Quantity**, and **Add to Job** from that panel
- Preserves all existing job data, routes, and backend functions
- Leaves manual used-part entry for the upcoming Parts Basket workspace
- Includes automatic template backup, validation, and rollback

## Install

Stop PLG, extract this package, and run:

```bash
python3 install_commit_0003A_2.py
```

Restart PLG:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Review the Dashboard and a Job Details page.
