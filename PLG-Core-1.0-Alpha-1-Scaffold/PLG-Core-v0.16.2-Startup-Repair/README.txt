PLG Core v0.16.2 — Startup Repair

Purpose:
- Moves `from __future__ import annotations` back to the correct position.
- Preserves shebang/encoding lines when present.
- Creates a backup before editing.
- Compiles app.py to verify the repair.
- Automatically restores the original file if validation fails.

Run:
python3 repair_v0162.py

Then restart PLG Core:
cd ~/Desktop/PLG-Core-Starter-v1
source .venv/bin/activate
./run.sh
