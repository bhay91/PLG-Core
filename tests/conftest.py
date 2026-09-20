"""Pytest configuration for PPS.

Runs before any test module is imported, so PPS_DB_PATH is set to an
isolated temp directory before legacy_app reads it at import time.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="pps-test-"))

(_TMP / "documents").mkdir(exist_ok=True)
(_TMP / "uploads").mkdir(exist_ok=True)

_seed = _ROOT / "data" / "plg_core.db"
if _seed.exists():
    shutil.copy2(_seed, _TMP / "plg_core.db")

os.environ.setdefault("PPS_DB_PATH", str(_TMP / "plg_core.db"))
os.environ.setdefault("PPS_DOCUMENT_ROOT", str(_TMP / "documents"))
os.environ.setdefault("PPS_UPLOAD_ROOT", str(_TMP / "uploads"))
