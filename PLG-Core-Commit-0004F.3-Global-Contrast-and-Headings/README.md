# PLG Commit 0004F.3 — Global Contrast & Headings

This applies the final typography update across the entire PLG website.

## Changes

- Keeps current body font sizes
- Enlarges all headings site-wide
- Changes primary gray-looking text to near-black
- Keeps disabled controls and placeholders muted
- Preserves all layouts, routes, imports, Basket behavior, checkout, pricing, and quote logic
- Includes automatic backup, validation, and rollback

## Install

Stop PLG, then run:

```bash
python3 install_commit_0004F_3.py
```

Restart PLG:

```bash
cd ~/Desktop/PLG-Core
source .venv/bin/activate
./run.sh
```

Review Dashboard, Jobs, New Job, one Job workspace, Vendor Quotes, and Selected Parts.

## Commit and push after approval

```bash
cd ~/Desktop/PLG-Core
git add .
git commit -m "Apply global contrast and heading polish"
git push
```
