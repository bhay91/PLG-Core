from pathlib import Path
import shutil
from datetime import datetime

SOURCE = Path(__file__).resolve().parent
TARGET = Path.home() / "PLG" / "Firefox-Extension"
TARGET.parent.mkdir(parents=True, exist_ok=True)

if TARGET.exists():
    backup = TARGET.with_name(
        TARGET.name + "-backup-sprint-1-" +
        datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    shutil.copytree(TARGET, backup)

for name in (
    "manifest.json", "sdk.js", "connectors.js", "content.js",
    "popup.html", "popup.js", "style.css",
):
    shutil.copy2(SOURCE / name, TARGET / name)

print("PLG Firefox Extension updated for Parts Basket imports.")
print("Reload PLG Verify Assistant in about:debugging.")
