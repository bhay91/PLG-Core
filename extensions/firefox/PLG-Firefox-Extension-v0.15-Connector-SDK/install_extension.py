from pathlib import Path
import shutil
from datetime import datetime

SOURCE = Path(__file__).resolve().parent
TARGET = Path.home() / "PLG" / "Firefox-Extension"

TARGET.parent.mkdir(parents=True, exist_ok=True)

if TARGET.exists():
    backup = TARGET.with_name(
        TARGET.name + "-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    shutil.copytree(TARGET, backup)
    shutil.rmtree(TARGET)

TARGET.mkdir(parents=True)

for name in (
    "manifest.json",
    "sdk.js",
    "connectors.js",
    "content.js",
    "popup.html",
    "popup.js",
    "style.css",
):
    shutil.copy2(SOURCE / name, TARGET / name)

print("PLG Firefox Extension v0.15 installed to:")
print(TARGET)
print("Load this file in Firefox:")
print(TARGET / "manifest.json")
