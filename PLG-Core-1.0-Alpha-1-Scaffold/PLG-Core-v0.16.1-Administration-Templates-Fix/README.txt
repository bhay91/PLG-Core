PLG Core v0.16.1 — Administration and Document Templates Fix

This corrects the v0.16 packaging path error.

Adds:
- Administration area
- Document Templates page
- Master PDF upload
- Automatic template versioning
- Active/inactive template history
- Preview and restore
- Approved corporate template seeded as version 1

Install:
1. Stop PLG Core.
2. Run: python3 install_v0161.py
3. Restart PLG Core.
4. Open Administration > Document Templates.

The installer is idempotent and safe if the failed v0.16 attempt already
modified app.py before stopping.
