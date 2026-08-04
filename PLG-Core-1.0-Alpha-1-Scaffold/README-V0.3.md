# PLG Core v0.3 Update

Adds:
- Edit customer and machine information at any time
- Active CAT verification session
- Verify in CAT button on each requested part
- Local API bridge for the Firefox extension
- Capture endpoint that writes OEM data directly into the selected job part

Install:
1. Stop the server.
2. Back up `data/plg_core.db`.
3. Copy this update into the existing PLG-Core-Starter-v1 folder.
4. Replace `app.py`, `templates/`, and `static/`.
5. Restart with `./run.sh`.
