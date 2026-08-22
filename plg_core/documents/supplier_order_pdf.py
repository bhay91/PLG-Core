from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
from xml.sax.saxutils import escape

from fastapi import HTTPException
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NAVY = colors.HexColor("#0D3563")
BLUE = colors.HexColor("#0868D7")
INK = colors.HexColor("#1D2A3B")
MUTED = colors.HexColor("#5E6D7E")
LINE = colors.HexColor("#D4DEE9")
SOFT = colors.HexColor("#F4F7FB")


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip(" .-") or "Supplier"


def supplier_order_path(order, *, version: int = 1, draft: bool = False) -> Path:
    root = Path(os.getenv("PPS_DOCUMENT_ROOT", str(PROJECT_ROOT / "documents")))
    directory = root / "Supplier Orders" / _safe_name(order["supplier_name"])
    directory.mkdir(parents=True, exist_ok=True)
    suffix = "-DRAFT" if draft else ("" if version == 1 else f"-v{version}")
    return directory / f"{_safe_name(order['po_number'])}{suffix}.pdf"


def load_supplier_order_document_data(
    connection: sqlite3.Connection, supplier_order_id: int
) -> dict:
    row = connection.execute(
        """
        SELECT po.*, j.job_number, j.customer, j.company,
               i.invoice_number,
               s.contact_person AS supplier_contact,
               s.phone AS supplier_phone,
               s.email AS supplier_email,
               s.account_number AS supplier_account
        FROM supplier_orders po
        JOIN jobs j ON j.id=po.job_id
        LEFT JOIN invoices i ON i.id=po.invoice_id
        LEFT JOIN suppliers s ON s.id=po.supplier_id
        WHERE po.id=?
        """,
        (supplier_order_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Supplier order not found.")
    items = connection.execute(
        """
        SELECT oi.*, ja.manufacturer AS asset_manufacturer,
               ja.model AS asset_model, ja.name AS asset_name,
               ja.vin_pin_serial AS asset_serial
        FROM supplier_order_items oi
        LEFT JOIN job_assets ja ON ja.id=oi.job_asset_id
        WHERE oi.order_id=?
        ORDER BY oi.id
        """,
        (supplier_order_id,),
    ).fetchall()
    result = dict(row)
    result["items"] = [dict(item) for item in items]
    return result


def _text(value) -> str:
    return escape(str(value or "").strip())


def _money(value, currency: str = "USD") -> str:
    prefix = "$" if str(currency or "USD").upper() == "USD" else f"{currency} "
    return f"{prefix}{float(value or 0):,.2f}"


def _date(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not set"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%b %d, %Y")
    except ValueError:
        return raw


def _asset_context(items: list[dict]) -> list[str]:
    contexts = []
    for item in items:
        values = [item.get("asset_manufacturer"), item.get("asset_model") or item.get("asset_name")]
        identity = " ".join(str(value).strip() for value in values if str(value or "").strip())
        serial = str(item.get("asset_serial") or "").strip()
        context = identity + (f" · {serial}" if serial else "")
        if context and context not in contexts:
            contexts.append(context)
    return contexts


def build_supplier_order_pdf(order: dict, output_path: Path, *, draft: bool) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("POTitle", parent=styles["Title"], textColor=NAVY, fontSize=20)
    label = ParagraphStyle("POLabel", parent=styles["Normal"], textColor=MUTED, fontSize=8, leading=10)
    value = ParagraphStyle("POValue", parent=styles["Normal"], textColor=INK, fontSize=9, leading=12)
    small = ParagraphStyle("POSmall", parent=value, fontSize=8, leading=10)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER, leftMargin=.42 * inch,
        rightMargin=.42 * inch, topMargin=.38 * inch, bottomMargin=.42 * inch,
    )
    logo = next((path for path in (
        PROJECT_ROOT / "static" / "pps-logo.png",
        PROJECT_ROOT / "static" / "plg-logo.webp",
        PROJECT_ROOT / "static" / "plg-logo.png",
    ) if path.is_file()), None)
    brand = Image(str(logo), width=2.3 * inch, height=.62 * inch) if logo else Paragraph("<b>PINPOINT SOURCING CO.</b>", value)
    status = "DRAFT — NOT ISSUED" if draft else str(order["status"] or "").upper()
    header = Table([[brand, [Paragraph("PURCHASE ORDER", title), Paragraph(f"<b>{_text(order['po_number'])}</b><br/>{_text(status)}", value)]]], colWidths=[4.4 * inch, 3.0 * inch])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    contact = " · ".join(filter(None, [str(order.get("supplier_contact") or "").strip(), str(order.get("supplier_phone") or "").strip(), str(order.get("supplier_email") or "").strip()]))
    customer = str(order.get("customer") or "").strip()
    if order.get("company"):
        customer += f" · {order['company']}"
    assets = _asset_context(order["items"])
    machine_text = "Multiple job assets" if len(assets) > 1 else (assets[0] if assets else "Not assigned")
    info_rows = [
        [Paragraph("SUPPLIER", label), Paragraph("PO DATE", label), Paragraph("ORDER STATUS", label)],
        [Paragraph(_text(order["supplier_name"]), value), Paragraph(_text(_date(order.get("ordered_at") or order.get("created_at"))), value), Paragraph(_text(status), value)],
        [Paragraph(_text(contact or "No supplier contact stored"), small), "", ""],
        [Paragraph("JOB / INVOICE", label), Paragraph("CUSTOMER", label), Paragraph("MACHINE / PIN / SERIAL", label)],
        [Paragraph(_text(f"{order.get('job_number') or '—'} · {order.get('invoice_number') or '—'}"), value), Paragraph(_text(customer or "—"), value), Paragraph(_text(machine_text), value)],
    ]
    info = Table(info_rows, colWidths=[2.45 * inch, 2.25 * inch, 2.7 * inch])
    info.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .5, LINE), ("INNERGRID", (0, 0), (-1, -1), .25, LINE), ("BACKGROUND", (0, 0), (-1, 0), SOFT), ("BACKGROUND", (0, 3), (-1, 3), SOFT), ("SPAN", (0, 2), (-1, 2)), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    rows = [["QTY", "SUPPLIER PART #", "DESCRIPTION", "UNIT COST", "LINE TOTAL"]]
    for item in order["items"]:
        rows.append([
            str(item["quantity_ordered"]),
            Paragraph(_text(item.get("supplier_part_number") or "—"), small),
            Paragraph(_text(item.get("description") or "Part"), value),
            _money(item.get("unit_cost"), order.get("currency")),
            _money(item.get("line_cost"), order.get("currency")),
        ])
    items_table = Table(rows, repeatRows=1, colWidths=[.5 * inch, 1.45 * inch, 3.2 * inch, 1.1 * inch, 1.15 * inch])
    items_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7), ("GRID", (0, 0), (-1, -1), .4, LINE), ("ALIGN", (0, 1), (0, -1), "CENTER"), ("ALIGN", (3, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))

    totals = Table([
        ["Parts Total", _money(order.get("parts_total"), order.get("currency"))],
        ["Shipping", _money(order.get("shipping_total"), order.get("currency"))],
        ["ORDER TOTAL", _money(order.get("order_total"), order.get("currency"))],
    ], colWidths=[1.6 * inch, 1.3 * inch], hAlign="RIGHT")
    totals.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("TEXTCOLOR", (0, -1), (-1, -1), BLUE), ("LINEABOVE", (0, -1), (-1, -1), 1, NAVY), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    logistics = Table([
        [Paragraph("ORDERED DATE", label), Paragraph("EXPECTED ARRIVAL", label), Paragraph("CURRENCY", label)],
        [Paragraph(_text(_date(order.get("ordered_at"))), value), Paragraph(_text(_date(order.get("expected_at"))), value), Paragraph(_text(order.get("currency") or "USD"), value)],
    ], colWidths=[2.45 * inch] * 3)
    logistics.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .5, LINE), ("INNERGRID", (0, 0), (-1, -1), .25, LINE), ("BACKGROUND", (0, 0), (-1, 0), SOFT), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    notes = str(order.get("notes") or "").strip()
    story = [header, Spacer(1, 10), info, Spacer(1, 12), items_table, Spacer(1, 10), totals, Spacer(1, 12), logistics]
    if notes:
        story.extend([Spacer(1, 10), Paragraph("<b>SUPPLIER REFERENCE / NOTES</b>", label), Paragraph(_text(notes), value)])
    doc.build(story)
    return output_path
