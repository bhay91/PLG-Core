PLG v0.3.1 hotfix

What this fixes:
- Firefox extension stuck on “Loading active PLG verification…”
- Temporary Firefox extension UUID blocked by the prior CORS configuration
- Adds a timeout and clearer error message if PLG Core is not running

What this does NOT yet do:
- Open the exact CAT diagram or callout automatically.
- v0.3 opens CAT Parts and connects the correct PLG job/part.
- You still navigate to the correct diagram manually, then capture the OEM part.

Install:
1. Stop PLG Core.
2. Edit app.py using CORE-CORS-PATCH.txt.
3. Replace popup.js in the v0.3 extension folder with this hotfix popup.js.
4. Restart PLG Core.
5. In about:debugging, click Reload for PLG CAT Assistant.
