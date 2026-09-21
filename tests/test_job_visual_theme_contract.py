from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "static/job_workspace.css").read_text()


def test_dark_theme_covers_job_delivery_surfaces():
    for selector in (".delivery-context", ".delivery-command", ".delivery-availability", ".delivery-ready", ".delivery-history", ".delivery-result"):
        assert selector in CSS
    assert ':root[data-theme="dark"] .erp-app .jcc' in CSS


def test_dark_theme_covers_supplier_receiving_history():
    assert ':root[data-theme="dark"] .erp-app .jcc .receiving-history-card' in CSS
    assert "background: var(--jcc-surface) !important" in CSS


def test_dark_theme_covers_nested_options_and_delivery_summary_cells():
    for selector in (".jcc-options", ".jcc-option", ".jcc-quick-card", ".delivery-command-identity", ".delivery-command-state", ".delivery-command-action", ".delivery-command-quantity"):
        assert selector in CSS


def test_job_workspace_mobile_contract_remains_present():
    assert "@media (max-width: 520px)" in CSS
    assert "min-height: 44px" in CSS
