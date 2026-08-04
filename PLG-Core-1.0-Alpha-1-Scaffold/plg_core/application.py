from __future__ import annotations

from legacy_app import app


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "1.0-dev-build-001.1",
    }
