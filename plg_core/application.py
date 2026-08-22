from __future__ import annotations

from plg_core.version import PPS_PHASE, PPS_VERSION

from legacy_app import app

from plg_core.basket.routes import router as basket_router
from plg_core.machines.routes import router as machines_router
from plg_core.requests.routes import router as requests_router
from plg_core.requests.extension_routes import router as firefox_inbox_router
from plg_core.followups.routes import router as followups_router
from plg_core.assets.routes import router as assets_router
from plg_core.commercial.routes import router as commercial_router
from plg_core.verification.routes import router as verification_router
from plg_core.research.routes import router as research_router
from plg_core.intake.routes import router as intake_router
from plg_core.sources.routes import router as sources_router
from plg_core.disposable.routes import router as disposable_router
from plg_core.ai.routes import router as ai_router
from plg_core.mcp import register_mcp
from plg_core.database.migrations import run_migrations

app.include_router(basket_router)
app.include_router(machines_router)
app.include_router(requests_router)
app.include_router(firefox_inbox_router)
app.include_router(followups_router)
app.include_router(assets_router)
app.include_router(commercial_router)
app.include_router(verification_router)
app.include_router(research_router)
app.include_router(intake_router)
app.include_router(sources_router)
app.include_router(disposable_router)
app.include_router(ai_router)
register_mcp(app)


@app.on_event("startup")
def run_modular_migrations() -> None:
    run_migrations()


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": PPS_VERSION,
        "phase": PPS_PHASE,
    }

# BEGIN PPS ROADMAP ALPHA 12-19
from plg_core.admin.routes import router as roadmap_admin_router
from plg_core.core_api.middleware import install_optional_api_hardening
from plg_core.core_api.routes import router as roadmap_core_router
from plg_core.crm.routes import router as roadmap_crm_router
from plg_core.roadmap.migrations import run_roadmap_migrations
from plg_core.sales.routes import router as roadmap_sales_router
from plg_core.supply.routes import router as roadmap_supply_router

app.include_router(roadmap_sales_router)
app.include_router(roadmap_supply_router)
app.include_router(roadmap_crm_router)
app.include_router(roadmap_admin_router)
app.include_router(roadmap_core_router)

install_optional_api_hardening(app)

@app.on_event("startup")
def run_alpha_12_19_migrations() -> None:
    run_roadmap_migrations()
# END PPS ROADMAP ALPHA 12-19
