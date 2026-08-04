PLG Core v0.14.2 — Worldpac Final

Fixes the Worldpac shipping selector by pairing each summary label with
its next summary value.

Adds:
- Parts subtotal
- Shipping
- Supplier total
- Source-import cost summary on the PLG job page
- Automatic correction of the known Cameron King test import

Install:
1. Stop PLG Core.
2. Run: python3 install_v0142.py
3. Restart PLG Core.
4. Remove the old temporary extension.
5. Load extension/manifest.json.
