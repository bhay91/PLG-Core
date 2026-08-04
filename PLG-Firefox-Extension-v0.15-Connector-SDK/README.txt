PLG Firefox Extension v0.15 — Connector SDK

Architecture:
- sdk.js: shared parsers and normalization
- connectors.js: CAT SIS and Worldpac connectors
- content.js: connector detection and messaging
- popup.js: universal display and import

Money parser accepts:
857
857.8
857.80
857.825
$857.80
857.8 (USD) ea.

Install:
1. Run: python3 install_extension.py
2. Open about:debugging#/runtime/this-firefox
3. Remove the old PLG Verify Assistant
4. Load:
   /home/brandonbayleyhay/PLG/Firefox-Extension/manifest.json
