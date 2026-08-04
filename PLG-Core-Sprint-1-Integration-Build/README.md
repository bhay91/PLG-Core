# PLG Sprint 1 Integration Build

One combined test build containing:

- Parts Basket webpage
- Live supplier, shipping, customer, and profit totals
- Manual Basket entry
- Select/deselect and delete
- CAT SIS import into Basket
- Worldpac import into Basket
- Commit selected Basket items to the job
- Automatic source selection for the existing quote generator
- Firefox extension update
- Backup, validation, and rollback

## Core install

Stop PLG, then run:

```bash
python3 install_sprint_1.py
```

## Extension update

Inside the `extension` folder run:

```bash
python3 install_extension.py
```

Reload the temporary add-on in Firefox.

## End-to-end test

1. Open a real job.
2. Click **Parts Basket**.
3. Import CAT SIS.
4. Import Worldpac.
5. Confirm totals.
6. Commit selected items.
7. Return to the job.
8. Generate the quote.
