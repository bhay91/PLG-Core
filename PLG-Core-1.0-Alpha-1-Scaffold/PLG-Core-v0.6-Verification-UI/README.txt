PLG Core v0.6 — Verification UI

Changes:
- Hides OEM Quick Capture after a part is verified.
- Shows a green CAT VERIFIED summary.
- Adds machine-diagram and CAT-product links.
- Adds Re-Verify in CAT with confirmation.
- Keeps the existing verification saved until a new capture succeeds.
- Makes verified OEM number and description read-only.

Install:
1. Stop PLG Core with Ctrl+C.
2. Extract this ZIP.
3. Open a terminal inside the extracted folder.
4. Run:

   python3 install_v06.py

5. Restart PLG Core:

   cd ~/Desktop/PLG-Core-Starter-v1
   source .venv/bin/activate
   ./run.sh
