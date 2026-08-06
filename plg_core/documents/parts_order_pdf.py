from __future__ import annotations

from collections import OrderedDict
from datetime import date
from pathlib import Path
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


NAVY = colors.HexColor("#123A63")
BLUE = colors.HexColor("#1666D3")
INK = colors.HexColor("#24344D")
MUTED = colors.HexColor("#68778B")
LINE = colors.HexColor("#D8E1EB")
SOFT = colors.HexColor("#F3F6FA")
WHITE = colors.white

PAGE_SIZE = LETTER
LEFT_MARGIN = 0.32 * inch
RIGHT_MARGIN = 0.32 * inch
TOP_MARGIN = 0.30 * inch
BOTTOM_MARGIN = 0.52 * inch
CONTENT_WIDTH = 7.86 * inch


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
        Path("documents")
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
        "PartsLink Global · Internal Purchasing Document",
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
        author="PartsLink Global",
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
                width=1.12 * inch,
                height=0.55 * inch,
            )
        except Exception:
            logo_flow = Paragraph("PLG", styles["brand"])
    else:
        logo_flow = Paragraph("PLG", styles["brand"])

    business = [
        Paragraph("PARTSLINK GLOBAL", styles["brand"]),
        Paragraph(
            "Worldwide Parts Sourcing &amp; Logistics",
            styles["tagline"],
        ),
        Spacer(1, 2),
        Paragraph(
            "2033 W McNab Rd Ste S, Pompano Beach, FL 33069",
            styles["tagline"],
        ),
        Paragraph(
            "USA: +1 (561) 978-4452 · Jamaica: +1 (876) 429-0046",
            styles["tagline"],
        ),
    ]

    meta = [
        Paragraph("PARTS ORDER SHEET", styles["title"]),
        Spacer(1, 4),
        Table(
            [
                [
                    "Invoice",
                    str(_value(invoice, "invoice_number")),
                ],
                [
                    "Job",
                    str(_value(invoice, "job_number")),
                ],
                [
                    "Date",
                    date.today().isoformat(),
                ],
            ],
            colWidths=[0.62 * inch, 1.40 * inch],
            style=[
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.4),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ],
        ),
    ]

    table = Table(
        [[logo_flow, business, meta]],
        colWidths=[1.22 * inch, 4.12 * inch, 2.52 * inch],
    )

    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LINEBEFORE", (2, 0), (2, 0), 0.65, LINE),
        ("LEFTPADDING", (2, 0), (2, 0), 10),
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
            2.62 * inch,
            2.82 * inch,
            2.42 * inch,
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
        colWidths=[3.45 * inch, 4.41 * inch],
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
            colWidths=[6.15 * inch, 1.71 * inch],
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
