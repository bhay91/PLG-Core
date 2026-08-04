from pathlib import Path

service = Path("plg_core/basket/service.py")
text = service.read_text()

old = """                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1.0, ?)
"""
new = """                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, ?)
"""

if old not in text:
    if "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, ?)" in text:
        print("Import selection default already updated.")
    else:
        raise SystemExit("Could not locate imported-item selection default.")
else:
    service.write_text(text.replace(old, new, 1))
    print("Imported Vendor Cart items now wait for Add to Parts Basket.")
