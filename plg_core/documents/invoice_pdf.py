
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import os
import re
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    TopPadder,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from plg_core.documents.pdf_fit import (
    build_with_one_page_preference,
    fit_value,
    font_scale,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ROOT = (
    Path(os.getenv("PPS_DOCUMENT_ROOT", str(PROJECT_ROOT / "documents")))
    / "Customers"
)
LOGO_CANDIDATES = [
    PROJECT_ROOT / "static" / "pps-logo.png",
    PROJECT_ROOT / "static" / "plg-logo.webp",
    PROJECT_ROOT / "static" / "plg-logo.png",
    PROJECT_ROOT / "static" / "plg-logo.jpg",
]

NAVY = colors.HexColor("#0D3563")
BLUE = colors.HexColor("#0868D7")
INK = colors.HexColor("#1D2A3B")
MUTED = colors.HexColor("#5E6D7E")
LINE = colors.HexColor("#D4DEE9")
SOFT = colors.HexColor("#F4F7FB")
PALE_BLUE = colors.HexColor("#EAF2FC")
GREEN = colors.HexColor("#138A4B")
WHITE = colors.white

# PLG Corporate PDF V2
PDF_PAGE_SIZE = LETTER
PDF_LEFT_MARGIN = 0.28 * inch
PDF_RIGHT_MARGIN = 0.28 * inch
PDF_TOP_MARGIN = 0.27 * inch
PDF_BOTTOM_MARGIN = 1.22 * inch
PDF_CONTENT_WIDTH = 7.94 * inch


def sanitize_path_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "Unknown Customer"


def money(value) -> str:
    return f"${float(value or 0):,.2f}"


def invoice_paths(customer: str, invoice_number: str) -> dict[str, Path]:
    root = DOCUMENT_ROOT / sanitize_path_name(customer) / "Invoices"
    customer_dir = root / "Customer"
    internal_dir = root / "Internal"
    customer_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_path_name(invoice_number)
    return {
        "customer": customer_dir / f"{safe}.pdf",
        "internal": internal_dir / f"{safe}-Internal.pdf",
    }


def paid_invoice_paths(
    customer: str,
    invoice_number: str,
) -> dict[str, Path]:
    """Return separate paths for paid invoice copies."""

    root = (
        DOCUMENT_ROOT
        / sanitize_path_name(customer)
        / "Invoices"
    )
    customer_dir = root / "Customer"
    internal_dir = root / "Internal"

    customer_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)

    safe = sanitize_path_name(invoice_number)

    return {
        "customer": customer_dir / f"{safe}-PAID.pdf",
        "internal": internal_dir / f"{safe}-Internal-PAID.pdf",
    }


def _value(row, key, default=""):
    try:
        value = row[key]
    except Exception:
        value = getattr(row, key, default)
    return default if value in (None, "") else value


def _logo() -> Path | None:
    for path in LOGO_CANDIDATES:
        if path.exists():
            return path
    return None


def _logo_image(path: Path) -> Image:
    width, height = ImageReader(str(path)).getSize()
    scale = min((2.50 * inch) / width, (0.68 * inch) / height)
    return Image(str(path), width=width * scale, height=height * scale)


def _date_text(raw) -> str:
    value = str(raw or "").strip()
    if not value:
        return date.today().isoformat()
    return value


def _valid_until(raw_date) -> str:
    value = str(raw_date or "").strip()
    try:
        return (datetime.strptime(value, "%Y-%m-%d").date() + timedelta(days=30)).isoformat()
    except Exception:
        return (date.today() + timedelta(days=30)).isoformat()


def _styles():
    base = getSampleStyleSheet()
    scale = font_scale()
    sized = lambda value: value * scale
    return {
        "brand": ParagraphStyle(
            "PLGBrand", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=sized(20), leading=sized(22), textColor=NAVY,
            spaceAfter=fit_value(2, 1, 1, 0),
        ),
        "tagline": ParagraphStyle(
            "PLGTagline", parent=base["Normal"], fontName="Helvetica",
            fontSize=sized(9.2), leading=sized(11), textColor=INK,
        ),
        "contact": ParagraphStyle(
            "PLGContact", parent=base["Normal"], fontName="Helvetica",
            fontSize=sized(7.7), leading=sized(10), textColor=MUTED,
        ),
        "doc_title": ParagraphStyle(
            "PLGDocTitle", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=sized(26), leading=sized(28), alignment=TA_RIGHT, rightIndent=4, textColor=NAVY,
        ),
        "label": ParagraphStyle(
            "PLGLabel", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=sized(8.2), leading=sized(10), textColor=INK,
        ),
        "value": ParagraphStyle(
            "PLGValue", parent=base["Normal"], fontName="Helvetica",
            fontSize=sized(8.2), leading=sized(10), textColor=INK,
        ),
        "small": ParagraphStyle(
            "PLGSmall", parent=base["Normal"], fontName="Helvetica",
            fontSize=sized(8.0), leading=sized(10), textColor=MUTED,
        ),
        "footer_heading": ParagraphStyle(
            "PLGFooterHeading", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=sized(7.6), leading=sized(9), alignment=TA_CENTER, textColor=NAVY,
        ),
        "footer": ParagraphStyle(
            "PLGFooter", parent=base["Normal"], fontName="Helvetica",
            fontSize=sized(6.6), leading=sized(8.2), alignment=TA_CENTER, textColor=MUTED,
        ),
        "center_brand": ParagraphStyle(
            "PLGCenterBrand", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=sized(9), leading=sized(11), alignment=TA_CENTER, textColor=NAVY,
        ),
        "center_tag": ParagraphStyle(
            "PLGCenterTag", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=sized(6.8), leading=sized(8), alignment=TA_CENTER, textColor=MUTED,
        ),
    }


def _page_footer(canvas, doc, title, number):
    canvas.saveState()
    width, _ = PDF_PAGE_SIZE
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(
        PDF_LEFT_MARGIN,
        fit_value(0.39, 0.35, 0.32, 0.30) * inch,
        width - PDF_RIGHT_MARGIN,
        fit_value(0.39, 0.35, 0.32, 0.30) * inch,
    )
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(
        PDF_LEFT_MARGIN,
        fit_value(0.21, 0.18, 0.16, 0.14) * inch,
        "Pinpoint Sourcing Co. | Worldwide Parts Sourcing & Logistics",
    )
    canvas.drawRightString(
        width - PDF_RIGHT_MARGIN,
        fit_value(0.21, 0.18, 0.16, 0.14) * inch,
        f"{title} {number} | Page {doc.page}",
    )

    footer_blocks = _footer_blocks()
    footer_blocks.wrapOn(canvas, PDF_CONTENT_WIDTH, 0.70 * inch)
    footer_blocks.drawOn(
        canvas, PDF_LEFT_MARGIN,
        fit_value(0.47, 0.43, 0.39, 0.36) * inch,
    )

    canvas.restoreState()


def _document(path: Path, invoice, title: str):
    doc = BaseDocTemplate(
        str(path),
        pagesize=PDF_PAGE_SIZE,
        leftMargin=PDF_LEFT_MARGIN,
        rightMargin=PDF_RIGHT_MARGIN,
        topMargin=fit_value(0.27, 0.22, 0.18, 0.16) * inch,
        bottomMargin=fit_value(1.22, 1.10, 1.02, 0.96) * inch,
        title=f"{title} {_value(invoice, 'invoice_number')}",
        author="Pinpoint Sourcing Co.",
    )

    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id="main",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )

    doc.addPageTemplates([
        PageTemplate(
            id="corporate-v2",
            frames=[frame],
            onPage=lambda canvas, d: _page_footer(
                canvas,
                d,
                title,
                str(_value(invoice, "invoice_number")),
            ),
        )
    ])

    return doc

def _invoice_status_details(invoice):
    status = str(
        _value(invoice, "status", "UNPAID") or "UNPAID"
    ).strip().upper()

    if status == "PAID":
        return {
            "label": "PAID IN FULL",
            "color": colors.HexColor("#1F7A4F"),
        }

    if status == "PARTIAL":
        return {
            "label": "PARTIALLY PAID",
            "color": colors.HexColor("#B26A00"),
        }

    if status == "VOID":
        return {
            "label": "VOID",
            "color": colors.HexColor("#A53945"),
        }

    return {
        "label": "UNPAID",
        "color": colors.HexColor("#A53945"),
    }

def _header(invoice, internal: bool):
    s = _styles()
    logo = _logo()
    logo_flow = []
    if logo:
        try:
            logo_flow.append(_logo_image(logo))
        except Exception:
            pass
    if not logo_flow:
        logo_flow.append(Paragraph("PPS", ParagraphStyle(
            "FallbackLogo", parent=s["brand"], fontSize=30, leading=31
        )))

    business = [
        Paragraph(
            "2033 W McNab Rd Ste S, Pompano Beach, FL 33069",
            s["contact"],
        ),
        Paragraph("USA: +1 (561) 978-4452 &nbsp; | &nbsp; Jamaica: +1 (876) 429-0046", s["contact"]),
        Paragraph("info@pinpointsourcing.com", s["contact"]),
    ]

    title = "INTERNAL INVOICE" if internal else "INVOICE"
    status_details = _invoice_status_details(invoice)
    invoice_status = str(
        _value(invoice, "status", "UNPAID") or "UNPAID"
    ).strip().upper()

    meta_rows = [
        ["Date", _date_text(_value(invoice, "invoice_date"))],
    ]

    if invoice_status == "PAID":
        paid_date = _value(invoice, "paid_date", "")

        if paid_date:
            meta_rows.append([
                "Paid Date",
                _date_text(paid_date),
            ])
    else:
        meta_rows.append([
            "Valid Until",
            _valid_until(_value(invoice, "invoice_date")),
        ])

    meta_rows.append([
        "Status",
        Paragraph(
            status_details["label"],
            ParagraphStyle(
                "InvoiceStatusDisplay",
                parent=s["small"],
                fontName="Helvetica-Bold",
                fontSize=7.7,
                textColor=status_details["color"],
                alignment=TA_RIGHT,
            ),
        ),
    ])

    invoice_number_style = ParagraphStyle(
        "PPSInvoiceNumber",
        parent=s["small"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=13,
        textColor=BLUE,
        alignment=TA_RIGHT,
        rightIndent=4,
    )

    meta_rows_display = [["", row[0], row[1]] for row in meta_rows]

    meta = [
        Paragraph(title, s["doc_title"]),
        Spacer(1, 1),
        Paragraph(str(_value(invoice, "invoice_number")), invoice_number_style),
        Spacer(1, 5),
        Table(
            meta_rows_display,
            colWidths=[1.00 * inch, 0.60 * inch, 0.90 * inch],
            hAlign="RIGHT",
            style=[
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -2), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.7),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("ALIGN", (1, 0), (1, -1), "LEFT"),
            ("LEFTPADDING", (1, 0), (1, -1), 2),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ],
        ),
    ]

    company_block = logo_flow + [Spacer(1, 5)] + business

    table = Table(
        [[company_block, meta]],
        colWidths=[
            5.44 * inch,
            2.50 * inch,
        ],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ("LINEBEFORE", (2,0), (2,0), 0.7, LINE),
        ("LEFTPADDING", (2,0), (2,0), 10),
    ]))
    return table


def _info_box(title, rows, hide_empty=False):
    s = _styles()
    data = [[Paragraph(title, ParagraphStyle(
        f"{title}Header", parent=s["label"], fontSize=9.5, textColor=NAVY
    ))]]
    for label, value in rows:
        if hide_empty and not str(value or "").strip():
            continue

        data.append([
            Table([[
                Paragraph(f"{label}:", s["label"]),
                Paragraph(str(value or "Not Provided"), s["value"]),
            ]], colWidths=[0.95 * inch, 2.58 * inch], style=[
                ("LEFTPADDING", (0,0), (-1,-1), 0),
                ("RIGHTPADDING", (0,0), (-1,-1), 0),
                ("TOPPADDING", (0,0), (-1,-1), 1),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
            ])
        ])
    box = Table(data, colWidths=[3.92 * inch])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), PALE_BLUE),
        ("BOX", (0,0), (-1,-1), 0.65, LINE),
        ("LINEBELOW", (0,0), (-1,0), 0.65, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), fit_value(4, 3, 2, 2)),
        ("BOTTOMPADDING", (0,0), (-1,-1), fit_value(4, 3, 2, 2)),
    ]))
    return box


def _information(invoice, internal=False):
    customer = _info_box("CUSTOMER INFORMATION", [
        ("Customer", _value(invoice, "customer")),
        ("Company", _value(invoice, "company")),
        ("Address", _value(invoice, "address")),
        ("Phone", _value(invoice, "phone")),
        ("Email", _value(invoice, "email")),
    ], hide_empty=not internal)
    machine = _info_box("EQUIPMENT INFORMATION", [
        ("Manufacturer", _value(invoice, "manufacturer")),
        ("Model", _value(invoice, "machine")),
        ("Year", _value(invoice, "year")),
        ("VIN / PIN / Serial", _value(invoice, "pin_serial")),
    ], hide_empty=not internal)
    table = Table(
        [[customer, machine]],
        colWidths=[3.97 * inch, 3.97 * inch],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("LEFTPADDING", (1,0), (1,0), 5),
    ]))
    return table


def _section_bar(text):
    return Table([[text]], colWidths=[PDF_CONTENT_WIDTH], style=[
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("TEXTCOLOR", (0,0), (-1,-1), WHITE),
        ("FONTNAME", (0,0), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), fit_value(7, 5, 4, 3)),
        ("BOTTOMPADDING", (0,0), (-1,-1), fit_value(7, 5, 4, 3)),
    ])


def _customer_items(items):
    s = _styles()

    rows = [[
        "QTY",
        "PART NUMBER",
        "DESCRIPTION",
        "UNIT PRICE",
        "TOTAL",
    ]]

    for item in items:
        public_part_number = str(
            _value(item, "supplier_part_number", "") or ""
        ).strip()
        if not public_part_number:
            internal_reference = str(
                _value(item, "internal_part_number", "") or ""
            ).strip()
            public_part_number = (
                f"PPS Ref: {internal_reference}" if internal_reference else "—"
            )
        rows.append([
            str(_value(item, "quantity", 1)),
            Paragraph(
                public_part_number,
                s["small"],
            ),
            Paragraph(
                str(_value(item, "description", "Part")),
                s["value"],
            ),
            money(_value(item, "customer_unit_price", 0)),
            money(_value(item, "customer_line_total", 0)),
        ])

    table = Table(
        rows,
        colWidths=[
            0.45 * inch,
            1.28 * inch,
            3.82 * inch,
            1.08 * inch,
            1.31 * inch,
        ],
        repeatRows=1,
        splitByRow=1,
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.2),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 8.2),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        (
            "ROWBACKGROUNDS",
            (0, 1),
            (-1, -1),
            [WHITE, colors.HexColor("#FAFBFD")],
        ),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), fit_value(6, 4.5, 3.5, 3)),
        ("BOTTOMPADDING", (0, 0), (-1, -1), fit_value(6, 4.5, 3.5, 3)),
    ]))

    return table

def _internal_items(items):
    s = _styles()

    rows = [[
        "QTY",
        "DESCRIPTION",
        "VENDOR",
        "PART #",
        "COST",
        "SELL",
        "PROFIT",
    ]]

    for item in items:
        rows.append([
            str(_value(item, "quantity", 1)),
            Paragraph(
                str(_value(item, "description", "Part")),
                s["small"],
            ),
            Paragraph(
                str(_value(item, "supplier_name", "")),
                s["small"],
            ),
            Paragraph(
                str(_value(item, "supplier_part_number", "")),
                s["small"],
            ),
            money(_value(item, "supplier_line_total", 0)),
            money(_value(item, "customer_line_total", 0)),
            money(_value(item, "line_profit", 0)),
        ])

    table = Table(
        rows,
        colWidths=[
            0.38 * inch,
            2.62 * inch,
            1.08 * inch,
            1.12 * inch,
            0.90 * inch,
            0.90 * inch,
            0.94 * inch,
        ],
        repeatRows=1,
        splitByRow=1,
    )

    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 6.8),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 7.1),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        (
            "ROWBACKGROUNDS",
            (0, 1),
            (-1, -1),
            [WHITE, colors.HexColor("#FAFBFD")],
        ),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), fit_value(6, 4.5, 3.5, 3)),
        ("BOTTOMPADDING", (0, 0), (-1, -1), fit_value(6, 4.5, 3.5, 3)),
    ]))

    return table

def _payment_box():
    s = _styles()
    jmm = [
        Paragraph("JMMB (JAMAICA)", s["label"]),
        Spacer(1, 3),
        Paragraph("Bank: JMMB - Montego Bay", s["small"]),
        Paragraph("Account Name: Brandon Bayley-Hay", s["small"]),
        Paragraph("JMD Account: 008600020157", s["small"]),
        Paragraph("USD Account: 008600020156", s["small"]),
        Paragraph("Account Type: Savings", s["small"]),
    ]
    zelle = [
        Paragraph("ZELLE (USA)", s["label"]),
        Spacer(1, 3),
        Paragraph("Name: Brandon Bayley-Hay", s["small"]),
        Paragraph("Phone: +1 (561) 978-4452", s["small"]),
    ]
    inner = Table(
        [[jmm, zelle]],
        colWidths=[2.82 * inch, 1.82 * inch],
    )
    inner.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LINEBEFORE", (1,0), (1,0), 0.5, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), fit_value(7, 5, 4, 3)),
        ("BOTTOMPADDING", (0,0), (-1,-1), fit_value(7, 5, 4, 3)),
    ]))
    box = Table([
        [Paragraph("PAYMENT INFORMATION", ParagraphStyle(
            "PaymentHeader", parent=s["label"], fontSize=9.5, textColor=NAVY
        ))],
        [inner],
    ], colWidths=[4.82 * inch])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), PALE_BLUE),
        ("BOX", (0,0), (-1,-1), 0.65, LINE),
        ("LINEBELOW", (0,0), (-1,0), 0.65, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), fit_value(6, 4, 3, 2)),
        ("BOTTOMPADDING", (0,0), (-1,-1), fit_value(6, 4, 3, 2)),
    ]))
    return box


def _totals_box(invoice, internal: bool):
    shipping = float(
        _value(invoice, "shipping_total", 0) or 0
    )
    service_charge = float(
        _value(invoice, "service_charge", 0) or 0
    )
    sourcing_fee = float(
        _value(invoice, "sourcing_fee", 0) or 0
    )

    status = str(
        _value(invoice, "status", "UNPAID") or "UNPAID"
    ).strip().upper()

    balance_due = float(
        _value(invoice, "balance_due", 0) or 0
    )

    if internal:
        actual_state = str(_value(invoice, "actual_cost_state", "NOT_CONFIRMED"))
        actual_confirmed = actual_state != "NOT_CONFIRMED"
        rows = [
            ["Revenue", money(_value(invoice, "customer_total", 0))],
            ["Estimated Cost", money(_value(invoice, "booked_supplier_cost", _value(invoice, "supplier_total", 0)))],
            ["Supplier Order Cost", money(_value(invoice, "placed_supplier_cost", 0))],
            ["Final Actual Cost", money(_value(invoice, "actual_supplier_cost", 0)) if actual_confirmed else "NOT CONFIRMED"],
            ["Expected Profit", money(_value(invoice, "expected_profit", _value(invoice, "profit_total", 0)))],
            ["Supplier Order Profit", money(_value(invoice, "placed_cost_profit", 0))],
            ["Final Profit", money(_value(invoice, "actual_profit", 0)) if actual_confirmed else "NOT CONFIRMED"],
            ["Cost Difference", money(_value(invoice, "cost_variance", 0)) if actual_confirmed else "—"],
            ["Profit Difference", money(_value(invoice, "profit_variance", 0)) if actual_confirmed else "—"],
            ["Actual Cost Confirmation State", actual_state.replace("_", " ")],
        ]
        if service_charge > 0:
            rows.append(["Service Charge", money(service_charge)])
        if sourcing_fee > 0:
            rows.append(["Sourcing Fee", money(sourcing_fee)])

        if status == "PAID":
            rows.append(["PAYMENT STATUS", "PAID IN FULL"])
        elif status == "PARTIAL":
            rows.append(["BALANCE DUE", money(balance_due)])
    else:
        customer_subtotal = (
            float(_value(invoice, "customer_total", 0) or 0)
            - shipping
        )
        rows = [
            [
                "Subtotal",
                money(customer_subtotal),
            ],
        ]

        if shipping > 0:
            rows.append(["Shipping", money(shipping)])

        rows.append([
            "Invoice Total",
            money(_value(invoice, "customer_total", 0)),
        ])

        credit_applied = float(
            _value(invoice, "credit_applied", 0) or 0
        )

        if credit_applied > 0:
            rows.append([
                "Credit Applied",
                money(credit_applied),
            ])

        if status == "PAID":
            rows.append(["BALANCE DUE", money(balance_due)])
            rows.append(["PAID IN FULL", ""])
        else:
            rows.append(["BALANCE DUE", money(balance_due)])

    table = Table(
        rows,
        colWidths=[1.20 * inch, 1.92 * inch],
    )

    final_row = len(rows) - 1
    paid = status == "PAID"

    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), fit_value(6, 4, 3, 2)),
        ("BOTTOMPADDING", (0, 0), (-1, -1), fit_value(6, 4, 3, 2)),
        ("LINEBELOW", (0, 0), (-1, final_row - 1), 0.35, LINE),
        (
            "BACKGROUND",
            (0, final_row),
            (-1, final_row),
            colors.HexColor("#1F7A4F") if paid else NAVY,
        ),
        ("TEXTCOLOR", (0, final_row), (-1, final_row), WHITE),
        (
            "FONTNAME",
            (0, final_row),
            (-1, final_row),
            "Helvetica-Bold",
        ),
        ("FONTSIZE", (0, final_row), (-1, final_row), 11),
        ("TOPPADDING", (0, final_row), (-1, final_row), fit_value(8, 6, 5, 4)),
        ("BOTTOMPADDING", (0, final_row), (-1, final_row), fit_value(8, 6, 5, 4)),
    ]))

    if paid:
        table.setStyle(TableStyle([
            ("SPAN", (0, final_row), (1, final_row)),
            ("ALIGN", (0, final_row), (1, final_row), "CENTER"),
        ]))

    return table

def _bottom_blocks(invoice, internal):
    totals = _totals_box(invoice, internal)
    status = str(_value(invoice, "status", "UNPAID") or "UNPAID").strip().upper()

    if status == "PAID":
        return Table(
            [[totals]],
            colWidths=[3.12 * inch],
            hAlign="RIGHT",
            style=[
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("LEFTPADDING", (0,0), (-1,-1), 0),
                ("RIGHTPADDING", (0,0), (-1,-1), 0),
            ],
        )

    payment = _payment_box()
    table = Table(
        [[payment, totals]],
        colWidths=[4.82 * inch, 3.12 * inch],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("LEFTPADDING", (1,0), (1,0), 5),
    ]))
    return table


def _footer_blocks():
    s = _styles()
    left = [
        Paragraph("PARTS IDENTIFICATION", s["footer_heading"]),
        Spacer(1, fit_value(4, 3, 2, 1)),
        Paragraph(
            "Parts are supplied based on the vehicle or equipment information provided "
            "by the customer. Please verify compatibility before installation.",
            s["footer"],
        ),
    ]
    center = [
        Paragraph("THANK YOU FOR CHOOSING", s["footer_heading"]),
        Paragraph("PINPOINT SOURCING CO.", s["center_brand"]),
        Paragraph("Worldwide Parts Sourcing &amp; Logistics", s["center_tag"]),
    ]
    right = [
        Paragraph("WARRANTY INFORMATION", s["footer_heading"]),
        Spacer(1, fit_value(4, 3, 2, 1)),
        Paragraph(
            "Manufacturer warranty applies where provided by the original supplier.",
            s["footer"],
        ),
    ]
    table = Table(
        [[left, center, right]],
        colWidths=[
            2.646 * inch,
            2.648 * inch,
            2.646 * inch,
        ],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LINEABOVE", (0,0), (-1,0), 0.8, NAVY),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("RIGHTPADDING", (0,0), (-1,-1), 7),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    return table


def _invoice_notice(invoice, internal):
    s = _styles()
    status = str(_value(invoice, "status", "") or "").strip().upper()
    if internal:
        text = "INTERNAL USE ONLY - Supplier pricing and profit are confidential."
    elif status == "PAID":
        text = "Payment received in full. Thank you for your business."
    else:
        text = "Invoice valid for 30 days. Pricing and availability are subject to confirmation."
    return Table([[Paragraph(text, ParagraphStyle(
        "Notice", parent=s["small"], alignment=TA_CENTER,
        textColor=NAVY if not internal else colors.HexColor("#9B2C2C"),
        fontName="Helvetica-Bold"
    ))]], colWidths=[7.44*inch], style=[
        ("BACKGROUND", (0,0), (-1,-1), PALE_BLUE if not internal else colors.HexColor("#FFF1F1")),
        ("BOX", (0,0), (-1,-1), 0.5, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), fit_value(5, 4, 3, 2)),
        ("BOTTOMPADDING", (0,0), (-1,-1), fit_value(5, 4, 3, 2)),
    ])


def _build_invoice_pdf_once(
    invoice,
    items: Iterable,
    path: Path,
    internal: bool,
):
    items = list(items)
    path.parent.mkdir(parents=True, exist_ok=True)

    title = "INTERNAL INVOICE" if internal else "INVOICE"
    doc = _document(path, invoice, title)

    story = [
        _header(invoice, internal),
        Spacer(1, fit_value(5, 3, 2, 1)),

        Table(
            [[""]],
            colWidths=[PDF_CONTENT_WIDTH],
            style=[
                ("LINEBELOW", (0, 0), (-1, -1), 1.0, NAVY),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ],
        ),

        Spacer(1, fit_value(5, 3, 2, 1)),
        _information(invoice, internal),
        Spacer(1, fit_value(5, 3, 2, 1)),

        _section_bar(
            "PARTS & SERVICES"
            if not internal
            else "INTERNAL COST & PROFIT DETAIL"
        ),

        _internal_items(items)
        if internal
        else _customer_items(items),

        Spacer(1, fit_value(5, 3, 2, 1)),
        _invoice_notice(invoice, internal),
        Spacer(1, fit_value(5, 3, 2, 1)),
        _bottom_blocks(invoice, internal),
        Spacer(1, fit_value(2, 1, 1, 0)),
        ]

    doc.build(story)


def build_invoice_pdf(
    invoice,
    items: Iterable,
    path: Path,
    internal: bool,
):
    items = list(items)
    path = Path(path)
    return build_with_one_page_preference(
        path,
        lambda: _build_invoice_pdf_once(invoice, items, path, internal),
    )

def generate_paid_invoice_pdfs(
    invoice,
    items: Iterable,
) -> dict[str, str]:
    """Generate separate paid customer and internal invoice copies."""

    status = str(
        _value(invoice, "status", "") or ""
    ).strip().upper()

    if status != "PAID":
        raise ValueError(
            "Paid invoice PDFs require invoice status PAID."
        )

    items = list(items)

    paths = paid_invoice_paths(
        _value(invoice, "customer"),
        _value(invoice, "invoice_number"),
    )

    build_invoice_pdf(
        invoice,
        items,
        paths["customer"],
        internal=False,
    )

    build_invoice_pdf(
        invoice,
        items,
        paths["internal"],
        internal=True,
    )

    return {
        key: str(value)
        for key, value in paths.items()
    }


def generate_invoice_pdfs(invoice, items: Iterable) -> dict[str, str]:
    items = list(items)
    paths = invoice_paths(_value(invoice, "customer"), _value(invoice, "invoice_number"))
    build_invoice_pdf(invoice, items, paths["customer"], internal=False)
    build_invoice_pdf(invoice, items, paths["internal"], internal=True)
    return {key: str(value) for key, value in paths.items()}

def custom_invoice_path(invoice, custom_invoice) -> Path:
    """Return the saved PDF path for a Custom Invoice."""
    customer = sanitize_path_name(
        str(_value(invoice, "customer", "Customer"))
    )
    number = sanitize_path_name(
        str(_value(custom_invoice, "custom_invoice_number", "Custom"))
    )

    path = (
        Path(os.getenv("PPS_DOCUMENT_ROOT", "documents"))
        / "Customers"
        / customer
        / "Invoices"
        / "Custom"
        / f"{number}.pdf"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def generate_custom_invoice_pdf(
    invoice,
    custom_invoice,
    custom_items: Iterable,
    output_path: Path | None = None,
) -> str:
    """Generate a saved Custom Invoice using the locked PPS invoice layout."""
    from plg_core.documents.custom_invoice import custom_invoice_presentation

    presentation = custom_invoice_presentation(
        invoice, custom_invoice, custom_items
    )

    custom_invoice_data = dict(invoice)
    custom_invoice_data["invoice_number"] = _value(
        custom_invoice,
        "custom_invoice_number",
        _value(invoice, "invoice_number", ""),
    )
    custom_invoice_data["parts_subtotal"] = float(
        presentation["subtotal"]
    )
    custom_invoice_data["shipping_total"] = presentation["freight"]
    # Custom customer invoices intentionally roll these internal charges into
    # the final invoice amount instead of itemizing them.  Mutate only the
    # presentation copy so the source invoice retains its accounting data.
    custom_invoice_data["service_charge"] = 0.0
    custom_invoice_data["sourcing_fee"] = 0.0
    custom_invoice_data["customer_total"] = float(
        presentation["invoice_total"]
    )
    custom_invoice_data["credit_applied"] = 0.0
    custom_invoice_data["balance_due"] = presentation["balance_due"]
    custom_invoice_data["status"] = "PAID"

    items = []

    for item in presentation["visible_items"]:
        row = dict(item)
        row["customer_unit_price"] = float(
            _value(item, "custom_unit_price", 0) or 0
        )
        row["customer_line_total"] = float(
            _value(item, "custom_line_total", 0) or 0
        )
        items.append(row)

    path = output_path or custom_invoice_path(invoice, custom_invoice)

    build_invoice_pdf(
        custom_invoice_data,
        items,
        path,
        internal=False,
    )

    return str(path)
