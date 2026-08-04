from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
BASKET = PROJECT / "templates" / "basket.html"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"
SOURCE = Path(__file__).resolve().parent / "payload"

for path in (BASKET, CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004F.1-{stamp}"
backup.mkdir(parents=True)

for current in (BASKET, CSS):
    saved = backup / current.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore():
    for current in (BASKET, CSS):
        saved = backup / current.relative_to(PROJECT)
        if saved.exists():
            shutil.copy2(saved, current)

try:
    text = BASKET.read_text()

    # Normalize visible wording without depending on one exact previous heading.
    replacements = [
        (r"\bVendor Carts\b", "Vendor Quotes"),
        (r"\bVendor Cart\b", "Vendor Quote"),
        (r"\bvendor carts\b", "vendor quotes"),
        (r"\bvendor cart\b", "vendor quote"),
        (r"\bVENDOR CARTS\b", "VENDOR QUOTES"),
        (r"\bVENDOR CART\b", "VENDOR QUOTE"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)

    # Change the main import section label if the streamlined flow renamed it.
    text = text.replace(
        '<p class="eyebrow">IMPORT &amp; ADD PARTS</p>',
        '<p class="eyebrow">VENDOR QUOTES</p>',
        1,
    )

    # Make each vendor card collapsible, but only once.
    card_pattern = re.compile(
        r'<article class="vendor-cart-card">(.*?)</article>',
        re.DOTALL,
    )

    def wrap_card(match):
        inner = match.group(1)
        if "<details" in inner:
            return match.group(0)

        header = re.search(
            r'<header class="vendor-cart-header">(.*?)</header>',
            inner,
            re.DOTALL,
        )
        if not header:
            return match.group(0)

        header_inner = header.group(1)
        if "vendor-cart-chevron" not in header_inner:
            header_inner = header_inner.replace(
                "<div>",
                '<div class="vendor-cart-summary-copy"><span class="vendor-cart-chevron">›</span><div>',
                1,
            )
            header_inner = header_inner.replace(
                '</div>\n      <div class="vendor-cart-meta">',
                '</div></div>\n      <div class="vendor-cart-meta">',
                1,
            )

        new_header = f'<header class="vendor-cart-header">{header_inner}</header>'
        body = inner.replace(header.group(0), "", 1)

        return (
            '<article class="vendor-cart-card">'
            '<details open>'
            '<summary>'
            + new_header +
            '</summary>'
            + body +
            '</details>'
            '</article>'
        )

    text, card_count = card_pattern.subn(wrap_card, text)
    if card_count == 0:
        raise RuntimeError("No Vendor Quote cards were found.")

    BASKET.write_text(text)

    css = CSS.read_text()
    marker = "/* Commit 0004F.1 Vendor Quotes UI Fix */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004F.1.css").read_text()
        CSS.write_text(css)

    final_template = BASKET.read_text()
    final_css = CSS.read_text()

    if "vendor-cart-card" not in final_template:
        raise RuntimeError("Vendor Quote cards are missing.")
    if "<details open>" not in final_template:
        raise RuntimeError("Expandable Vendor Quote sections were not added.")
    if "vendor-cart-chevron" not in final_template:
        raise RuntimeError("Vendor Quote expand indicator is missing.")
    if marker not in final_css:
        raise RuntimeError("Vendor Quote typography CSS was not installed.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from jinja2 import Environment, FileSystemLoader; "
                "env=Environment(loader=FileSystemLoader('templates')); "
                "env.get_template('basket.html'); "
                "from app import app; "
                "assert app is not None; "
                "print('Vendor Quotes UI fix smoke test passed')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    restore()
    raise SystemExit(
        "Commit 0004F.1 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004F.1 Vendor Quotes UI Fix installed successfully.")
print("Vendor sections are expandable and collapsible.")
print("Vendor Quote typography now matches the rest of PLG.")
print(f"Backup created at: {backup}")
