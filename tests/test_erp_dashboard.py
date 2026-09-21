from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


def test_erp_dashboard_is_read_only_and_uses_real_dashboard_contract():
    source = (ROOT / "templates" / "operator_dashboard.html").read_text(encoding="utf-8")
    assert "<form" not in source
    for value in (
        "inbox_total", "jobs", "payments", "ordering", "receiving",
        "delivery", "accounting_exceptions", "report_date",
    ):
        assert value in source
    Environment(loader=FileSystemLoader(ROOT / "templates")).get_template("operator_dashboard.html")


def test_erp_dashboard_has_metric_grid_panels_and_dense_jobs_table():
    source = (ROOT / "templates" / "operator_dashboard.html").read_text(encoding="utf-8")
    for component in (
        "erp-metric-grid", "erp-dashboard-grid", "erp-action-panel",
        "erp-needs-panel", "erp-flow-panel", "erp-inbox-strip",
        "erp-jobs-snapshot", "erp-table",
    ):
        assert component in source
    for heading in ("Action Queue", "Requested Needs", "Order Movement", "Active Jobs Snapshot"):
        assert heading in source


def test_erp_shell_uses_local_svg_icons_and_existing_routes_only():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    assert "erp-icon-sprite" in base
    assert "<symbol id=\"erp-i-dashboard\"" in base
    assert "pps_erp.css" in base
    assert "http://" not in base and "https://" not in base
    for route in ("/", "/jobs", "/requests", "/quotes", "/purchasing", "/invoices", "/documents"):
        assert f'href="{route}"' in base
