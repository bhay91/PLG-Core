# PLG Commit 0003B.1 — Dashboard & Job Workspace Fix

This fixes only the installer validation for the FastAPI router behavior on your Fedora installation.

The prior installer rolled back safely because the Basket route is stored inside the included Basket router rather than directly in `app.routes`.

## Install

Stop PLG, then run:

```bash
python3 install_commit_0003B.py
```

Restart:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Review the Dashboard and open one Job.
