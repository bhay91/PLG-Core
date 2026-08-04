from __future__ import annotations

from legacy_app import app

from plg_core.basket.routes import router as basket_router
from plg_core.database.migrations import run_migrations

app.include_router(basket_router)


@app.on_event("startup")
def run_modular_migrations() -> None:
    run_migrations()


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "1.0.0-alpha.1-commit-0002",
    }
