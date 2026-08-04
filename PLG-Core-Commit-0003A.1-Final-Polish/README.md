# PLG Commit 0003A.1 — Final Corporate Shell Polish

This finishes the approved PLG corporate look.

## Changes

- Removed the duplicate **New Job** button from the top-right header
- Kept the **New Job** action in the left sidebar
- Changed the subtitle to **Parts Sourcing Control Center**
- Applied a crisper application-wide font stack
- Refined font weights, spacing, kerning, and text rendering
- Preserved all current PLG routes and business logic
- Includes automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0003A_1.py
```

Restart PLG:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Open:

`http://127.0.0.1:8000`

Review Dashboard, Jobs, Job Details, Connectors, and Administration. Once approved, commit and push the UI changes.
