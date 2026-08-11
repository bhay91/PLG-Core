from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path
import os
from typing import Iterable
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


NAVY = colors.HexColor("#0D3563")
BLUE = colors.HexColor("#0868D7")
INK = colors.HexColor("#1D2A3B")
MUTED = colors.HexColor("#5E6D7E")
LINE = colors.HexColor("#D4DEE9")
SOFT = colors.HexColor("#F4F7FB")
WHITE = colors.white

PAGE_SIZE = LETTER
LEFT_MARGIN = 0.28 * inch
RIGHT_MARGIN = 0.28 * inch
TOP_MARGIN = 0.27 * inch
BOTTOM_MARGIN = 1.22 * inch
CONTENT_WIDTH = 7.94 * inch


def _value(row, key: str, default=""):
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(row, key, default)

    return default if value is None else value


def _safe_name(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*]+', "-", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .-") or "Unknown"


def _money(value) -> str:
    return f"${float(value or 0):,.2f}"


def _logo_path() -> Path | None:
    candidates = (
        Path("static/pps-logo.png"),
        Path("static/plg-logo.webp"),
        Path("static/plg-logo.png"),
        Path("static/logo.png"),
    )

    for path in candidates:
        if path.exists():
            return path

    return None


def parts_order_sheet_path(invoice) -> Path:
    customer = _safe_name(_value(invoice, "customer", "Unknown Customer"))
    number = _safe_name(_value(invoice, "invoice_number", "Invoice"))

    return (
        Path(os.getenv("PPS_DOCUMENT_ROOT", "documents"))
        / "Customers"
        / customer
        / "Parts Order Sheets"
        / f"{number}-Parts-Order-Sheet.pdf"
    )


def _styles():
    base = getSampleStyleSheet()

    return {
        "brand": ParagraphStyle(
            "PartsOrderBrand",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=19,
            textColor=NAVY,
        ),
        "tagline": ParagraphStyle(
            "PartsOrderTagline",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=MUTED,
        ),
        "title": ParagraphStyle(
            "PartsOrderTitle",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=19,
            alignment=TA_RIGHT,
            textColor=NAVY,
        ),
        "label": ParagraphStyle(
            "PartsOrderLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=8.5,
            textColor=MUTED,
        ),
        "value": ParagraphStyle(
            "PartsOrderValue",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=10.5,
            textColor=INK,
        ),
        "small": ParagraphStyle(
            "PartsOrderSmall",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.2,
            leading=9,
            textColor=INK,
        ),
        "supplier": ParagraphStyle(
            "PartsOrderSupplier",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=WHITE,
        ),
        "footer": ParagraphStyle(
            "PartsOrderFooter",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=6.5,
            leading=8,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
    }


def _page_footer(canvas, doc, invoice_number: str):
    canvas.saveState()

    width, _ = PAGE_SIZE

    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.7)
    canvas.line(
        LEFT_MARGIN,
        0.39 * inch,
        width - RIGHT_MARGIN,
        0.39 * inch,
    )

    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(MUTED)

    canvas.drawString(
        LEFT_MARGIN,
        0.21 * inch,
        "Pinpoint Sourcing Co. · Internal Purchasing Document",
    )

    canvas.drawRightString(
        width - RIGHT_MARGIN,
        0.21 * inch,
        f"{invoice_number} · Page {doc.page}",
    )

    canvas.restoreState()


def _document(path: Path, invoice):
    invoice_number = str(_value(invoice, "invoice_number"))

    doc = BaseDocTemplate(
        str(path),
        pagesize=PAGE_SIZE,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=f"Parts Order Sheet {invoice_number}",
        author="Pinpoint Sourcing Co.",
    )

    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        id="parts-order-main",
    )

    doc.addPageTemplates([
        PageTemplate(
            id="parts-order-sheet",
            frames=[frame],
            onPage=lambda canvas, current_doc: _page_footer(
                canvas,
                current_doc,
                invoice_number,
            ),
        )
    ])

    return doc


def _header(invoice):
    styles = _styles()
    logo = _logo_path()

    if logo:
        try:
            logo_flow = Image(
                str(logo),
                width=2.50 * inch,
                height=0.68 * inch,
            )
        except Exception:
            logo_flow = Paragraph("PPS", styles["brand"])
    else:
        logo_flow = Paragraph("PPS", styles["brand"])

    business = [
        Paragraph(
            "2033 W McNab Rd Ste S, Pompano Beach, FL 33069",
            styles["tagline"],
        ),
        Paragraph(
            "USA: +1 (561) 978-4452 · Jamaica: +1 (876) 429-0046",
            styles["tagline"],
        ),
    ]

    document_number_style = ParagraphStyle(
        "PPSPartsOrderNumber",
        parent=styles["small"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=13,
        textColor=BLUE,
        alignment=TA_RIGHT,
        rightIndent=4,
    )

    meta_rows = [
        ["Invoice", str(_value(invoice, "invoice_number"))],
        ["Job", str(_value(invoice, "job_number"))],
        ["Date", date.today().isoformat()],
    ]
    meta_rows_display = [["", row[0], row[1]] for row in meta_rows]

    meta = [
        Paragraph("PARTS ORDER SHEET", styles["title"]),
        Spacer(1, 1),
        Paragraph(
            str(_value(invoice, "job_number") or _value(invoice, "invoice_number")),
            document_number_style,
        ),
        Spacer(1, 5),
        Table(
            meta_rows_display,
            colWidths=[0.85 * inch, 0.65 * inch, 1.16 * inch],
            hAlign="RIGHT",
            style=[
                ("FONTNAME", (1,0), (1,-1), "Helvetica-Bold"),
                ("FONTNAME", (2,0), (2,-1), "Helvetica"),
                ("FONTSIZE", (0,0), (-1,-1), 7.7),
                ("TEXTCOLOR", (1,0), (1,-1), INK),
                ("ALIGN", (1,0), (1,-1), "LEFT"),
                ("LEFTPADDING", (1,0), (1,-1), 2),
                ("ALIGN", (2,0), (2,-1), "RIGHT"),
                ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
                ("TOPPADDING", (0,0), (-1,-1), 2),
                ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ],
        ),
    ]

    company_block = [logo_flow, Spacer(1, 5)] + business

    table = Table(
        [[company_block, meta]],
        colWidths=[5.28 * inch, 2.66 * inch],
    )

    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ("LEFTPADDING", (0,0), (0,0), 8),
    ]))

    return table


def _job_information(invoice):
    styles = _styles()

    machine = " ".join(
        value
        for value in (
            str(_value(invoice, "manufacturer", "")).strip(),
            str(_value(invoice, "machine", "")).strip(),
        )
        if value
    ) or "Not provided"

    rows = [
        [
            Paragraph("CUSTOMER", styles["label"]),
            Paragraph("MACHINE / EQUIPMENT", styles["label"]),
            Paragraph("VIN / PIN / SERIAL", styles["label"]),
        ],
        [
            Paragraph(
                str(_value(invoice, "customer", "Not provided")),
                styles["value"],
            ),
            Paragraph(machine, styles["value"]),
            Paragraph(
                str(_value(invoice, "pin_serial", "Not provided")),
                styles["value"],
            ),
        ],
    ]

    table = Table(
        rows,
        colWidths=[
            2.64 * inch,
            2.92 * inch,
            2.38 * inch,
        ],
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("BOX", (0, 0), (-1, -1), 0.55, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    return table


def _supplier_header(name: str, details: dict):
    styles = _styles()

    contact_parts = []

    if details.get("contact_person"):
        contact_parts.append(str(details["contact_person"]))

    if details.get("phone"):
        contact_parts.append(str(details["phone"]))

    if details.get("email"):
        contact_parts.append(str(details["email"]))

    contact = " · ".join(contact_parts)

    right = Paragraph(
        contact or "No supplier contact details saved",
        ParagraphStyle(
            "PartsOrderSupplierContact",
            parent=styles["small"],
            alignment=TA_RIGHT,
            textColor=WHITE,
        ),
    )

    table = Table(
        [[Paragraph(name, styles["supplier"]), right]],
        colWidths=[3.52 * inch, 4.42 * inch],
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    return table


def _supplier_items(items: list):
    styles = _styles()

    rows = [[
        "QTY",
        "PART NUMBER",
        "DESCRIPTION",
        "UNIT COST",
        "LINE TOTAL",
    ]]

    subtotal = 0.0

    for item in items:
        quantity = max(1, int(_value(item, "quantity", 1) or 1))
        unit_cost = float(_value(item, "supplier_unit_cost", 0) or 0)
        line_total = float(
            _value(
                item,
                "supplier_line_total",
                unit_cost * quantity,
            )
            or 0
        )

        subtotal += line_total

        rows.append([
            str(quantity),
            Paragraph(
                str(
                    _value(item, "supplier_part_number", "")
                    or "—"
                ),
                styles["small"],
            ),
            Paragraph(
                str(_value(item, "description", "Part")),
                styles["value"],
            ),
            _money(unit_cost),
            _money(line_total),
        ])

    rows.append([
        "",
        "",
        Paragraph("<b>SUPPLIER SUBTOTAL</b>", styles["value"]),
        "",
        _money(subtotal),
    ])

    table = Table(
        rows,
        colWidths=[
            0.46 * inch,
            1.32 * inch,
            3.78 * inch,
            1.10 * inch,
            1.20 * inch,
        ],
        repeatRows=1,
        splitByRow=1,
    )

    last_row = len(rows) - 1

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("GRID", (0, 0), (-1, last_row - 1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE", (2, last_row), (-1, last_row), 0.8, NAVY),
        ("FONTNAME", (2, last_row), (-1, last_row), "Helvetica-Bold"),
        ("BACKGROUND", (2, last_row), (-1, last_row), SOFT),
    ]))

    return table, subtotal


def generate_parts_order_sheet(
    invoice,
    items: Iterable,
    supplier_details: dict[str, dict] | None = None,
) -> str:
    items = list(items)
    supplier_details = supplier_details or {}

    path = parts_order_sheet_path(invoice)
    path.parent.mkdir(parents=True, exist_ok=True)

    grouped: OrderedDict[str, list] = OrderedDict()

    for item in items:
        supplier = str(
            _value(item, "supplier_name", "")
            or "Supplier Not Assigned"
        ).strip()

        grouped.setdefault(supplier, []).append(item)

    doc = _document(path, invoice)
    styles = _styles()

    story = [
        _header(invoice),
        Spacer(1, 5),
        Table(
            [[""]],
            colWidths=[CONTENT_WIDTH],
            style=[
                ("LINEBELOW", (0, 0), (-1, -1), 1.0, NAVY),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ],
        ),
        Spacer(1, 6),
        _job_information(invoice),
        Spacer(1, 8),
    ]

    grand_total = 0.0

    for index, (supplier, supplier_items) in enumerate(grouped.items()):
        if index:
            story.append(Spacer(1, 8))

        details = supplier_details.get(supplier.lower(), {})

        story.append(_supplier_header(supplier, details))

        table, subtotal = _supplier_items(supplier_items)
        grand_total += subtotal

        story.append(table)

    story.extend([
        Spacer(1, 9),
        Table(
            [
                [
                    Paragraph(
                        "TOTAL SUPPLIER COST",
                        ParagraphStyle(
                            "PartsOrderGrandLabel",
                            parent=styles["value"],
                            fontName="Helvetica-Bold",
                            alignment=TA_RIGHT,
                            textColor=WHITE,
                        ),
                    ),
                    Paragraph(
                        _money(grand_total),
                        ParagraphStyle(
                            "PartsOrderGrandValue",
                            parent=styles["value"],
                            fontName="Helvetica-Bold",
                            fontSize=11,
                            alignment=TA_RIGHT,
                            textColor=WHITE,
                        ),
                    ),
                ]
            ],
            colWidths=[6.23 * inch, 1.71 * inch],
            style=[
                ("BACKGROUND", (0, 0), (-1, -1), NAVY),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ],
        ),
        Spacer(1, 8),
        Paragraph(
            (
                "Internal purchasing document. Verify part numbers, "
                "quantities, supplier pricing, and availability before ordering."
            ),
            styles["footer"],
        ),
    ])

    doc.build(story)

    return str(path)
