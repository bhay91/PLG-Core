from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_manual_intake_actions_follow_numbered_sections():
    template = source("templates/request_form.html")

    assert ".request-create-form>.request-create-optional{order:6}" in template
    assert ".request-create-form>.request-create-actions{order:7}" in template
    assert template.index('class="request-create-step">1') < template.index('class="request-create-step">2')


def test_job_summary_keeps_primary_action_and_demotes_need_research():
    template = source("templates/job_command_center_advanced.html")

    assert "{{ operational_snapshot.workflow.next_action }}" in template
    assert 'href="{{ operational_snapshot.workflow.next_url }}"' in template
    assert 'class="requested-need-research"' in template
    assert "Focus research on this need" in template
    assert "Research this Need" not in template
    assert "Requested: see wording" not in template
    assert "#research-results" in template


def test_fulfillment_histories_use_compact_disclosures():
    receiving = source("templates/supplier_order_detail.html")
    delivery = source("templates/job_delivery.html")
    styles = source("static/app.css")

    assert '<details class="receiving-history-card"' in receiving
    assert "This Receipt" in receiving
    assert "Total Received" in receiving
    assert '<details class="delivery-history-card"' in delivery
    assert "delivery.quantity_total" in delivery
    assert "delivery-history-card > summary" in styles
    assert "receiving-history-card > summary" in styles


def test_supplier_order_action_names_its_destination():
    template = source("templates/supplier_order_detail.html")

    assert "View Order History" in template
    assert "Open Delivery" in template
    assert "Receive Items" in template
    assert ">Continue</a>" not in template


def test_document_summary_has_distinct_operator_metrics():
    template = source("templates/documents.html")

    assert template.count('<article class="docs-card">') == 3
    assert "Manifest-backed" not in template
    assert "Integrity Issues" in template
    assert 'action="/documents" class="docs-filters"' in template
    assert "Document Details" in template
    assert ".docs-context .docs-mobile-label{display:none}" in template


def test_visual_polish_layer_is_loaded_last_and_stays_presentation_only():
    base = source("templates/base.html")
    polish = source("static/polish.css")

    assert base.index("app_v2.css") < base.index("polish.css")
    assert "PPS visual polish v1" in polish
    assert "@media (max-width: 720px)" in polish
    assert ".workspace-status-badge" in polish
    assert ".dashboard-section" in polish
    assert ".requested-need-primary" in polish
    assert ".purchase-ops-next" in polish
    assert ".delivery-history-card" in polish
    assert ".docs-summary" in polish
    assert ".request-create-section" in polish
    assert "animation:" not in polish
    assert "linear-gradient" not in polish
