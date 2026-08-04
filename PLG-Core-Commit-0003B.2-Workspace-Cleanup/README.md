# PLG Commit 0003B.2 — Workspace Cleanup

This applies the fixes identified during the Dashboard and Job Workspace review.

## Changes

- Removes the duplicate **New Job** button from the Dashboard heading
- Keeps **New Job** in the left sidebar and Quick Actions
- Removes duplicate **Parts Basket** and **Edit Job Information** buttons from the top Job heading
- Keeps the main **Open Parts Basket** workspace action
- Keeps the status selector and Update button
- Tightens the stacked operational panels to reduce scrolling
- Preserves existing routes, data, imports, quotes, and parts
- Includes automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0003B_2.py
```

Restart PLG:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Review the Dashboard and one Job page.
