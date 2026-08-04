PLG Core v0.11 — SIS Cart Import

Adds:
- Import SIS Cart button on CAT jobs
- Reads every visible SIS cart row
- Imports OEM number, description, quantity, price, and availability
- Matches existing parts by OEM number
- Creates new job parts when no match exists
- Creates CAT SIS OEM supplier options automatically
- Preserves single-part SIS verification

Install Core:
1. Stop PLG Core with Ctrl+C.
2. Open a terminal in this extracted folder.
3. Run: python3 install_v011.py
4. Restart PLG Core.

Install Extension:
1. Open about:debugging#/runtime/this-firefox
2. Remove the old PLG Verify Assistant.
3. Load Temporary Add-on.
4. Select extension/manifest.json

Cart workflow:
1. Create/open a CAT job.
2. Click Import SIS Cart.
3. Build or open the SIS cart.
4. Open PLG Verify Assistant.
5. Click Read SIS Cart.
6. Review the line items.
7. Click Import All Cart Items.
