from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
ROUTES = PROJECT / "plg_core" / "basket" / "routes.py"
BASKET = PROJECT / "templates" / "basket.html"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"
EXTENSION = Path.home() / "Desktop" / "PLG-Firefox-Extension-v0.15-Connector-SDK"
POPUP = EXTENSION / "popup.js"
SOURCE = Path(__file__).resolve().parent / "payload"

for path in (ROUTES, BASKET, CSS, PYTHON, POPUP):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004E-{stamp}"
backup.mkdir(parents=True)

managed = [ROUTES, BASKET, CSS, POPUP]
for current in managed:
    saved = backup / ("extension/popup.js" if current == POPUP else str(current.relative_to(PROJECT)))
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore():
    for current in managed:
        saved = backup / ("extension/popup.js" if current == POPUP else str(current.relative_to(PROJECT)))
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)

try:
    popup = POPUP.read_text()
    popup = popup.replace('api("/api/import-source-cart"', 'api("/api/basket/import-source-cart"')
    popup = popup.replace('`http://127.0.0.1:8000/jobs/${data.job_id}` +', '`http://127.0.0.1:8000/jobs/${data.job_id}/basket` +')
    popup = popup.replace('`http://127.0.0.1:8000/jobs/${data.job_id}`', '`http://127.0.0.1:8000/jobs/${data.job_id}/basket`')
    popup = popup.replace('`http://localhost:8000/jobs/${data.job_id}` +', '`http://localhost:8000/jobs/${data.job_id}/basket` +')
    popup = popup.replace('`http://localhost:8000/jobs/${data.job_id}`', '`http://localhost:8000/jobs/${data.job_id}/basket`')
    if "/api/basket/import-source-cart" not in popup:
        raise RuntimeError("Extension endpoint update failed.")
    if '/jobs/${data.job_id}/basket' not in popup:
        raise RuntimeError("Extension return URL update failed.")
    POPUP.write_text(popup)

    routes = ROUTES.read_text()
    marker = '@router.post("/jobs/{job_id}/basket/items/{item_id}/quantity")'
    if marker not in routes:
        insertion = '\n@router.post("/jobs/{job_id}/basket/items/{item_id}/quantity")\ndef update_item_quantity(\n    job_id: int,\n    item_id: int,\n    quantity: Annotated[int, Form()],\n):\n    update_item(\n        item_id,\n        BasketItemUpdate(quantity=max(1, quantity)),\n    )\n    return RedirectResponse(\n        url=f"/jobs/{job_id}/basket",\n        status_code=303,\n    )\n\n'
        at = routes.find('@router.post("/jobs/{job_id}/basket/items/{item_id}/delete")')
        if at == -1:
            raise RuntimeError("Basket delete route not found.")
        routes = routes[:at] + insertion + routes[at:]
        ROUTES.write_text(routes)

    basket = BASKET.read_text()
    old_qty = '<td class="numeric">{{ item.quantity }}</td>'
    replacement = '<td class="numeric">\n            <form class="basket-qty-form" method="post" action="/jobs/{{ job.id }}/basket/items/{{ item.id }}/quantity">\n              <input class="basket-qty-input" type="number" name="quantity" value="{{ item.quantity }}" min="1" required>\n              <button class="basket-qty-save" type="submit">Update</button>\n            </form>\n          </td>'
    if old_qty in basket:
        basket = basket.replace(old_qty, replacement, 1)
    elif "basket-qty-form" not in basket:
        raise RuntimeError("Final Basket quantity cell not found.")
    BASKET.write_text(basket)

    css = CSS.read_text()
    css_marker = "/* Commit 0004E Vendor Import and Quantity */"
    if css_marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004E.css").read_text()
        CSS.write_text(css)

    smoke = subprocess.run(
        [str(PYTHON), "-c",
         "from app import app; from plg_core.basket.routes import router; "
         "paths={r.path for r in router.routes}; "
         "assert '/jobs/{job_id}/basket/items/{item_id}/quantity' in paths; "
         "assert '/jobs/{job_id}/basket' in paths; print('ok')"],
        cwd=PROJECT, text=True, capture_output=True
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    restore()
    raise SystemExit(f"Commit 0004E failed and files were restored.\n{exc}")

print("PLG Commit 0004E installed successfully.")
print("Vendor imports now go to the Basket endpoint and return to the Parts Basket.")
print("Parts Basket quantity is editable.")
print(f"Backup created at: {backup}")
print("Reload the Firefox extension before testing.")
