# PLG Commit 0003B.4 — Parts Workspace Refresh

This updates the large parts section shown on the Job page.

## Included

- Renames it to **Parts Sourcing Workspace**
- Adds a clearer description
- Modern requested-part cards
- Cleaner OEM verification summary
- Compact supplier options
- Clear selected-source highlighting
- Stronger price emphasis
- Better add-supplier form
- Responsive layout
- Existing verification, source selection, delete, and quote logic preserved
- Automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0003B_4.py
```

Restart:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Open the same Job and scroll to the Parts Sourcing Workspace.
