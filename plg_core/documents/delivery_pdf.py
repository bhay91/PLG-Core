from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NAVY = colors.HexColor("#0D3563")
INK = colors.HexColor("#1D2A3B")
MUTED = colors.HexColor("#5E6D7E")
LINE = colors.HexColor("#D4DEE9")
SOFT = colors.HexColor("#F4F7FB")


def _safe(value) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip(" .-") or "Delivery"


def _text(value) -> str:
    return escape(str(value or "").strip())


def _date(value) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%b %d, %Y %H:%M")
    except ValueError:
        return raw or "Not recorded"


def delivery_note_path(delivery: dict) -> Path:
    root = Path(os.getenv("PPS_DOCUMENT_ROOT", str(PROJECT_ROOT / "documents")))
    directory = root / "Deliveries" / _safe(delivery.get("customer"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_safe(delivery.get('delivery_number'))}.pdf"


def build_delivery_pdf(delivery: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("DeliveryTitle", parent=styles["Title"], textColor=NAVY, fontSize=20)
    label = ParagraphStyle("DeliveryLabel", parent=styles["Normal"], textColor=MUTED, fontSize=8)
    value = ParagraphStyle("DeliveryValue", parent=styles["Normal"], textColor=INK, fontSize=9, leading=12)
    small = ParagraphStyle("DeliverySmall", parent=value, fontSize=8, leading=10)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER, leftMargin=.5 * inch, rightMargin=.5 * inch,
        topMargin=.45 * inch, bottomMargin=.5 * inch,
        title=f"Delivery Note {delivery.get('delivery_number')}",
    )
    header = Table([[
        Paragraph("<b>PINPOINT SOURCING CO.</b>", value),
        [Paragraph("DELIVERY NOTE", title), Paragraph(f"<b>{_text(delivery.get('delivery_number'))}</b>", value)],
    ]], colWidths=[3.1 * inch, 4.4 * inch])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    info_rows = [
        ["DELIVERY DATE", "CUSTOMER / RECIPIENT", "JOB / INVOICE"],
        [_date(delivery.get("delivery_date")),
         f"{delivery.get('customer') or 'Customer'} · {delivery.get('recipient') or '—'}",
         f"{delivery.get('job_number') or '—'} · {delivery.get('invoice_number') or '—'}"],
        ["ASSET / EQUIPMENT CONTEXT", "STATUS", "NOTES"],
        [delivery.get("machine_context") or "Not applicable", delivery.get("status") or "DELIVERED",
         delivery.get("notes") or "No delivery notes."],
    ]
    info = Table(
        [[Paragraph(_text(cell), label if index in (0, 2) else value) for cell in row]
         for index, row in enumerate(info_rows)], colWidths=[2.5 * inch] * 3,
    )
    info.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), .5, LINE), ("INNERGRID", (0, 0), (-1, -1), .25, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT), ("BACKGROUND", (0, 2), (-1, 2), SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    rows = [["ITEM / REFERENCE", "DESCRIPTION", "SUPPLIER PO", "QUANTITY DELIVERED"]]
    for item in delivery.get("items", []):
        rows.append([
            Paragraph(_text(item.get("supplier_part_number") or "—"), small),
            Paragraph(_text(item.get("description") or "Item"), small),
            Paragraph(_text(item.get("po_number") or "—"), small),
            str(item.get("quantity_delivered") or 0),
        ])
    items = Table(rows, repeatRows=1, colWidths=[1.45*inch,3.25*inch,1.45*inch,1.35*inch])
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("GRID", (0, 0), (-1, -1), .4, LINE), ("ALIGN", (3, 1), (3, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    doc.build([header, Spacer(1, 14), info, Spacer(1, 14), items])
    return output_path
