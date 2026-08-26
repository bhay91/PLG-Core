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
    return re.sub(r"\s+", " ", value).strip(" .-") or "Receipt"


def _text(value) -> str:
    return escape(str(value or "").strip())


def _date(value) -> str:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%b %d, %Y %H:%M"
        )
    except ValueError:
        return raw or "Not recorded"


def receiving_summary_path(receipt: dict) -> Path:
    root = Path(os.getenv("PPS_DOCUMENT_ROOT", str(PROJECT_ROOT / "documents")))
    directory = root / "Receiving" / _safe(receipt.get("supplier_name"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_safe(receipt.get('receipt_number'))}.pdf"


def _machine(receipt: dict) -> str:
    values = []
    for item in receipt.get("items", []):
        identity = " ".join(
            str(value).strip()
            for value in (
                item.get("asset_manufacturer"),
                item.get("asset_model") or item.get("asset_name"),
            )
            if str(value or "").strip()
        )
        serial = str(item.get("asset_serial") or "").strip()
        context = identity + (f" · {serial}" if serial else "")
        if context and context not in values:
            values.append(context)
    return "Multiple job assets" if len(values) > 1 else (values[0] if values else "Not assigned")


def build_receiving_pdf(receipt: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ReceivingTitle", parent=styles["Title"], textColor=NAVY, fontSize=20)
    label = ParagraphStyle("ReceivingLabel", parent=styles["Normal"], textColor=MUTED, fontSize=8)
    value = ParagraphStyle("ReceivingValue", parent=styles["Normal"], textColor=INK, fontSize=9, leading=12)
    small = ParagraphStyle("ReceivingSmall", parent=value, fontSize=8, leading=10)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER, leftMargin=.42 * inch,
        rightMargin=.42 * inch, topMargin=.38 * inch, bottomMargin=.42 * inch,
        title=f"Receiving Summary {receipt.get('receipt_number')}",
    )
    header = Table([[
        Paragraph("<b>PINPOINT SOURCING CO.</b>", value),
        [Paragraph("RECEIVING SUMMARY", title), Paragraph(
            f"<b>{_text(receipt.get('receipt_number'))}</b><br/>{_text(receipt.get('status_after'))}", value
        )],
    ]], colWidths=[3.2 * inch, 4.2 * inch])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    info_data = [
        ["RECEIPT DATE / TIME", "RECEIVER", "STATUS"],
        [_date(receipt.get("received_at")), receipt.get("receiver") or "Not recorded", receipt.get("status_after")],
        ["SUPPLIER / PO", "JOB / INVOICE", "ASSET / EQUIPMENT CONTEXT"],
        [
            f"{receipt.get('supplier_name') or '—'} · {receipt.get('po_number') or '—'}",
            f"{receipt.get('job_number') or '—'} · {receipt.get('invoice_number') or '—'}",
            _machine(receipt),
        ],
    ]
    info = Table(
        [[Paragraph(_text(cell), label if row in (0, 2) else value) for cell in line]
         for row, line in enumerate(info_data)],
        colWidths=[2.45 * inch] * 3,
    )
    info.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), .5, LINE), ("INNERGRID", (0, 0), (-1, -1), .25, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), SOFT), ("BACKGROUND", (0, 2), (-1, 2), SOFT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    rows = [["ITEM / REFERENCE", "DESCRIPTION", "QTY ORDERED", "THIS RECEIPT", "TOTAL RECEIVED", "REMAINING"]]
    for item in receipt.get("items", []):
        rows.append([
            Paragraph(_text(item.get("supplier_part_number") or "—"), small),
            Paragraph(_text(item.get("description") or "Item"), small),
            str(item.get("quantity_ordered") or 0),
            str(item.get("quantity_received") or 0),
            str(item.get("cumulative_received") or 0),
            str(item.get("remaining") or 0),
        ])
    items = Table(rows, repeatRows=1, colWidths=[1.25*inch,2.45*inch,.82*inch,.88*inch,.9*inch,.8*inch])
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 6.5),
        ("GRID", (0, 0), (-1, -1), .4, LINE), ("ALIGN", (2, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story = [header, Spacer(1, 12), info, Spacer(1, 12), items]
    notes = str(receipt.get("notes") or "").strip()
    if notes:
        story.extend([
            Spacer(1, 12), Paragraph("RECEIVING NOTES", label),
            Paragraph(_text(notes), value),
        ])
    doc.build(story)
    return output_path
