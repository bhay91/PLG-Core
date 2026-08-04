# PLG Commit 0006B — Customer Accounts

Adds:
- Customer transaction ledger
- Record payment
- Record refund
- Record signed adjustment
- Live available credit
- Live outstanding balance
- Total payments
- Complete account history

Accounting rule:
- Positive net balance = available customer credit
- Negative net balance = outstanding amount due

Install:
```bash
python3 install_commit_0006B.py
```

Restart PLG and open a customer account.
