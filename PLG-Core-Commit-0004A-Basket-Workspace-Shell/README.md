# PLG Commit 0004A — Parts Basket Workspace Shell

This is the first small step of the full Parts Basket rebuild.

## Included

- Table layout matching the approved PLG mockup
- Customer, machine, VIN, and Basket-status cards
- Supplier, part number, description, type, quantity, cost, markup, and customer-price columns
- Select and deselect controls
- Delete control
- Horizontal totals bar
- Back to Job
- Clear Basket
- Commit Selected to Job
- Manual **Add Part** dialog
- Dedicated **Add Used Part** dialog
- Existing Basket routes and pricing logic preserved
- Automatic backup, validation, and rollback

## Not included yet

The next small steps will add detailed used-part condition, notes, photos, warranty information, and supplier comparison expansion.

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004A.py
```

Restart:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Open a Job and click **Open Parts Basket**.
