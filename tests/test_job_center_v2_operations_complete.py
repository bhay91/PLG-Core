from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_job_center_normal_path_exposes_post_invoice_and_order_actions():
    source = (ROOT / "templates" / "job_center_v2.html").read_text()
    assert 'action="/jobs/{{ job.id }}/center/invoice"' in source
    assert 'action="/jobs/{{ job.id }}/center/supplier-orders"' in source
    assert 'name="csrf_token"' in source
    assert 'name="quote_id"' in source
    assert 'name="invoice_id"' in source


def test_job_center_has_no_advanced_basket_dependency_in_normal_template():
    source = (ROOT / "templates" / "job_center_v2.html").read_text()
    assert "view=advanced" not in source
    assert "Job Command Center" not in source
    assert "/jobs/{{ job.id }}/center?tab=job#parts" in source


def test_operational_mutation_adapters_are_post_only_and_csrf_protected():
    source = (ROOT / "plg_core" / "basket" / "routes.py").read_text()
    invoice = source[source.index('def center_create_invoice'):source.index('def center_create_supplier_orders')]
    orders = source[source.index('def center_create_supplier_orders'):source.index('def center_edit_customer')]
    assert '@router.post("/jobs/{job_id}/center/invoice")' in source
    assert '@router.post("/jobs/{job_id}/center/supplier-orders")' in source
    assert 'require_valid_csrf(request, csrf_token)' in invoice
    assert 'require_valid_csrf(request, csrf_token)' in orders
    assert 'convert_quote_to_invoice(int(quote_id))' in invoice
    assert 'create_orders_from_paid_invoice(int(invoice_id))' in orders


def test_job_center_keeps_authoritative_usd_and_currency_snapshot_read_model():
    source = (ROOT / "templates" / "job_center_v2.html").read_text()
    assert "USD authoritative" in source
    assert "invoice_presentation" in source
    assert "operational_snapshot.movement.received_units" in source
    assert "operational_snapshot.movement.delivered_units" in source
    assert "Open delivery workflow" in source


def test_jobs_list_targets_canonical_job_center():
    source = (ROOT / "templates" / "jobs.html").read_text()
    assert 'href="/jobs/{{ job.id }}/center"' in source
    assert 'href="/jobs/{{ job.id }}/basket?view=advanced"' not in source


def test_dashboard_job_entries_target_canonical_job_center():
    source = (ROOT / "templates" / "operator_dashboard.html").read_text()
    assert "'/jobs/' ~ item.job_id ~ '/center'" in source


def test_legacy_job_detail_entry_redirects_to_job_center():
    import legacy_app

    response = legacy_app.job_detail(_Request(), 17)
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/17/center"


class _Request:
    pass


def _connection_with_row(table, job_id, record_id):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, job_id INTEGER)")
    connection.execute(f"INSERT INTO {table}(id, job_id) VALUES (?, ?)", (record_id, job_id))
    return connection


def test_invoice_adapter_delegates_only_after_job_ownership_check(monkeypatch):
    import plg_core.basket.routes as routes

    seen = []
    monkeypatch.setattr(routes, "get_connection", lambda: _connection_with_row("quotes", 7, 42))
    monkeypatch.setattr("plg_core.web_security.require_valid_csrf", lambda request, token: seen.append(token))
    monkeypatch.setattr("legacy_app.convert_quote_to_invoice", lambda quote_id: seen.append(quote_id))

    response = routes.center_create_invoice(_Request(), 7, 42, "csrf")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/7/center")
    assert seen == ["csrf", 42]

    seen.clear()
    monkeypatch.setattr(routes, "get_connection", lambda: _connection_with_row("quotes", 8, 42))
    with pytest.raises(Exception):
        routes.center_create_invoice(_Request(), 7, 42, "csrf")
    assert seen == ["csrf"]


def test_supplier_order_adapter_delegates_only_after_job_ownership_check(monkeypatch):
    import plg_core.basket.routes as routes

    seen = []
    monkeypatch.setattr(routes, "get_connection", lambda: _connection_with_row("invoices", 7, 9))
    monkeypatch.setattr("plg_core.web_security.require_valid_csrf", lambda request, token: seen.append(token))
    monkeypatch.setattr("plg_core.supply.service.create_orders_from_paid_invoice", lambda invoice_id: seen.append(invoice_id))

    response = routes.center_create_supplier_orders(_Request(), 7, 9, "csrf")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/7/center")
    assert seen == ["csrf", 9]

    seen.clear()
    monkeypatch.setattr(routes, "get_connection", lambda: _connection_with_row("invoices", 8, 9))
    with pytest.raises(Exception):
        routes.center_create_supplier_orders(_Request(), 7, 9, "csrf")
    assert seen == ["csrf"]
