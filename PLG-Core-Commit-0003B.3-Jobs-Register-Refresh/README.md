# PLG Commit 0003B.3 — Jobs Register Refresh

This updates the page opened from **Jobs** in the left sidebar.

## Included

- Consistent Jobs page heading and description
- One **New Job** action
- Total, Requested, Researching, and Verified summary cards
- Live browser search
- Status filter
- Modern clickable job rows
- Customer and company
- Machine or vehicle
- VIN, PIN, or serial
- Verification count and progress bar
- Status badges
- Responsive layout
- Existing routes, job data, and workflow preserved
- Automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0003B_3.py
```

Restart:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Open **Jobs** from the left sidebar.
