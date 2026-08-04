# PLG Core v0.2 Update

This update adds:

- Professional left sidebar layout
- Improved dashboard
- Verified-parts progress on job tables
- OEM verification fields for every requested part
- Diagram/group and callout capture
- Source URL and verification notes
- Supplier, cost, and availability fields
- Automatic job status changes:
  - REQUESTED → RESEARCHING when some parts are verified
  - VERIFIED when all parts have OEM part numbers

## Apply the update

1. Stop the running server with Ctrl+C.
2. Back up your database:
   `cp data/plg_core.db data/plg_core-backup.db`
3. Copy these update files into your existing PLG-Core-Starter-v1 folder.
4. Allow overwrite when asked.
5. Restart with `./run.sh`.

Your existing `data/plg_core.db` is not included in this update and should remain in place.
