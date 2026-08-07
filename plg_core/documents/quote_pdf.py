
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ROOT = PROJECT_ROOT / "documents" / "Customers"
LOGO_CANDIDATES = [
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


def sanitize_path_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "Unknown Customer"


def money(value) -> str:
    return f"${float(value or 0):,.2f}"


def quote_paths(customer: str, quote_number: str) -> dict[str, Path]:
    root = DOCUMENT_ROOT / sanitize_path_name(customer) / "Quotes"
    customer_dir = root / "Customer"
    internal_dir = root / "Internal"
    customer_dir.mkdir(parents=True, exist_ok=True)
    internal_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_path_name(quote_number)
    return {
        "customer": customer_dir / f"{safe}.pdf",
        "internal": internal_dir / f"{safe}-Internal.pdf",
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
    return {
        "brand": ParagraphStyle(
            "PLGBrand", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=20, leading=22, textColor=NAVY, spaceAfter=2,
        ),
        "tagline": ParagraphStyle(
            "PLGTagline", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.2, leading=11, textColor=INK,
        ),
        "contact": ParagraphStyle(
            "PLGContact", parent=base["Normal"], fontName="Helvetica",
            fontSize=7.7, leading=10, textColor=MUTED,
        ),
        "doc_title": ParagraphStyle(
            "PLGDocTitle", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=26, leading=28, alignment=TA_RIGHT, textColor=NAVY,
        ),
        "label": ParagraphStyle(
            "PLGLabel", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=8, leading=10, textColor=INK,
        ),
        "value": ParagraphStyle(
            "PLGValue", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.7, leading=11, textColor=INK,
        ),
        "small": ParagraphStyle(
            "PLGSmall", parent=base["Normal"], fontName="Helvetica",
            fontSize=7.2, leading=9, textColor=MUTED,
        ),
        "footer_heading": ParagraphStyle(
            "PLGFooterHeading", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=7.6, leading=9, alignment=TA_CENTER, textColor=NAVY,
        ),
        "footer": ParagraphStyle(
            "PLGFooter", parent=base["Normal"], fontName="Helvetica",
            fontSize=6.6, leading=8.2, alignment=TA_CENTER, textColor=MUTED,
        ),
        "center_brand": ParagraphStyle(
            "PLGCenterBrand", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9, leading=11, alignment=TA_CENTER, textColor=NAVY,
        ),
        "center_tag": ParagraphStyle(
            "PLGCenterTag", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=6.8, leading=8, alignment=TA_CENTER, textColor=MUTED,
        ),
    }


def _page_footer(canvas, doc, title, number):
    canvas.saveState()
    width, _ = LETTER
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(0.8)
    canvas.line(0.43 * inch, 0.47 * inch, width - 0.43 * inch, 0.47 * inch)
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.44 * inch, 0.28 * inch, "PLG Corporate Document Standard v1.0")
    canvas.drawRightString(width - 0.44 * inch, 0.28 * inch, f"{title} {number} | Page {doc.page}")
    canvas.restoreState()


def _document(path: Path, quote, title: str):
    doc = BaseDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=0.43 * inch, rightMargin=0.43 * inch,
        topMargin=0.38 * inch, bottomMargin=0.63 * inch,
        title=f"{title} {_value(quote, 'quote_number')}",
        author="Pinpoint Sourcing Co.",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([
        PageTemplate(
            id="corporate",
            frames=[frame],
            onPage=lambda canvas, d: _page_footer(
                canvas, d, title, str(_value(quote, "quote_number"))
            ),
        )
    ])
    return doc


def _header(quote, internal: bool):
    s = _styles()
    logo = _logo()
    logo_flow = []
    if logo:
        try:
            logo_flow.append(Image(str(logo), width=1.28 * inch, height=0.63 * inch))
        except Exception:
            pass
    if not logo_flow:
        logo_flow.append(Paragraph("PLG", ParagraphStyle(
            "FallbackLogo", parent=s["brand"], fontSize=30, leading=31
        )))

    business = [
        Paragraph("PINPOINT SOURCING CO.", s["brand"]),
        Paragraph("Worldwide Parts Sourcing &amp; Logistics", s["tagline"]),
        Spacer(1, 3),
        Paragraph("2033 W McNab Rd Ste S, Pompano Beach, FL 33069", s["contact"]),
        Paragraph("USA: +1 (561) 978-4452 &nbsp; | &nbsp; Jamaica: +1 (876) 429-0046", s["contact"]),
        Paragraph("partslinkglobal@icloud.com", s["contact"]),
    ]

    title = "INTERNAL QUOTE" if internal else "QUOTE"
    meta = [
        Paragraph(title, s["doc_title"]),
        Spacer(1, 4),
        Table([
            ["Quote No.", str(_value(quote, "quote_number"))],
            ["Date", _date_text(_value(quote, "quote_date"))],
            ["Valid Until", _valid_until(_value(quote, "quote_date"))],
            ["Status", str(_value(quote, "status", "DRAFT"))],
        ], colWidths=[0.78 * inch, 1.28 * inch], style=[
            ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
            ("FONTNAME", (1,0), (1,-1), "Helvetica"),
            ("FONTSIZE", (0,0), (-1,-1), 7.7),
            ("TEXTCOLOR", (0,0), (-1,-1), INK),
            ("ALIGN", (1,0), (1,-1), "RIGHT"),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("TEXTCOLOR", (1,-1), (1,-1), BLUE),
            ("FONTNAME", (1,-1), (1,-1), "Helvetica-Bold"),
        ]),
    ]

    table = Table(
        [[logo_flow, business, meta]],
        colWidths=[1.42 * inch, 3.72 * inch, 2.18 * inch],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("TOPPADDING", (0,0), (-1,-1), 0),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ("LINEBEFORE", (2,0), (2,0), 0.7, LINE),
        ("LEFTPADDING", (2,0), (2,0), 14),
    ]))
    return table


def _info_box(title, rows):
    s = _styles()
    data = [[Paragraph(title, ParagraphStyle(
        f"{title}Header", parent=s["label"], fontSize=9.5, textColor=NAVY
    ))]]
    for label, value in rows:
        data.append([
            Table([[
                Paragraph(f"{label}:", s["label"]),
                Paragraph(str(value or "Not Provided"), s["value"]),
            ]], colWidths=[0.83 * inch, 2.7 * inch], style=[
                ("LEFTPADDING", (0,0), (-1,-1), 0),
                ("RIGHTPADDING", (0,0), (-1,-1), 0),
                ("TOPPADDING", (0,0), (-1,-1), 1),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
            ])
        ])
    box = Table(data, colWidths=[3.67 * inch])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), PALE_BLUE),
        ("BOX", (0,0), (-1,-1), 0.65, LINE),
        ("LINEBELOW", (0,0), (-1,0), 0.65, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    return box


def _information(quote):
    customer = _info_box("CUSTOMER INFORMATION", [
        ("Customer", _value(quote, "customer")),
        ("Company", _value(quote, "company", "Individual Customer")),
        ("Address", _value(quote, "address", "Not Provided")),
        ("Phone", _value(quote, "phone", "Not Provided")),
        ("Email", _value(quote, "email", "Not Provided")),
    ])
    machine = _info_box("EQUIPMENT INFORMATION", [
        ("Manufacturer", _value(quote, "manufacturer", "Not Provided")),
        ("Model", _value(quote, "machine", "Not Provided")),
        ("Year", _value(quote, "year", "Not Provided")),
        ("VIN / PIN / Serial", _value(quote, "pin_serial", "Not Provided")),
    ])
    table = Table([[customer, machine]], colWidths=[3.72 * inch, 3.72 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("LEFTPADDING", (1,0), (1,0), 8),
    ]))
    return table



def _customer_information(quote):
    """Compact customer-facing identity block."""
    s = _styles()

    customer_lines = [
        Paragraph("CUSTOMER", s["label"]),
        Spacer(1, 5),
        Paragraph(
            str(_value(quote, "customer", "Not Provided")),
            s["value"],
        ),
    ]

    company = str(_value(quote, "company", "") or "").strip()
    address = str(_value(quote, "address", "") or "").strip()
    phone = str(_value(quote, "phone", "") or "").strip()

    if company and company.lower() != "individual customer":
        customer_lines.append(Paragraph(company, s["small"]))

    if address:
        customer_lines.append(Paragraph(address, s["small"]))

    if phone:
        customer_lines.append(Paragraph(phone, s["small"]))

    manufacturer = str(
        _value(quote, "manufacturer", "") or ""
    ).strip()
    model = str(_value(quote, "machine", "") or "").strip()
    identifier = str(
        _value(quote, "pin_serial", "") or ""
    ).strip()

    machine_name = " ".join(
        value
        for value in (manufacturer, model)
        if value
    ).strip() or "Not Provided"

    machine_lines = [
        Paragraph("MACHINE", s["label"]),
        Spacer(1, 5),
        Paragraph(machine_name, s["value"]),
    ]

    if identifier:
        machine_lines.append(
            Paragraph(
                f"VIN / PIN / Serial: {identifier}",
                s["small"],
            )
        )

    table = Table(
        [[customer_lines, machine_lines]],
        colWidths=[3.72 * inch, 3.72 * inch],
    )

    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.55, LINE),
        ("LINEBEFORE", (1, 0), (1, 0), 0.55, LINE),
        ("BACKGROUND", (0, 0), (-1, -1), WHITE),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))

    return table

def _section_bar(text):
    return Table([[text]], colWidths=[7.44 * inch], style=[
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("TEXTCOLOR", (0,0), (-1,-1), WHITE),
        ("FONTNAME", (0,0), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
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
        rows.append([
            str(_value(item, "quantity", 1)),
            Paragraph(
                str(
                    _value(
                        item,
                        "supplier_part_number",
                        "",
                    )
                    or "-"
                ),
                s["small"],
            ),
            Paragraph(
                str(_value(item, "description", "Part")),
                s["value"],
            ),
            money(
                _value(
                    item,
                    "customer_unit_price",
                    0,
                )
            ),
            money(
                _value(
                    item,
                    "customer_line_total",
                    0,
                )
            ),
        ])

    table = Table(
        rows,
        colWidths=[
            0.48 * inch,
            1.25 * inch,
            3.25 * inch,
            1.10 * inch,
            1.36 * inch,
        ],
        repeatRows=1,
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
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("MINROWHEIGHT", (0, 1), (-1, -1), 28),
    ]))

    return table

def _internal_items(items):
    s = _styles()
    rows = [["QTY", "DESCRIPTION", "VENDOR", "PART #", "COST", "SELL", "PROFIT"]]
    for item in items:
        rows.append([
            str(_value(item, "quantity", 1)),
            Paragraph(str(_value(item, "description", "Part")), s["small"]),
            Paragraph(str(_value(item, "supplier_name", "")), s["small"]),
            Paragraph(str(_value(item, "supplier_part_number", "")), s["small"]),
            money(_value(item, "supplier_line_total", 0)),
            money(_value(item, "customer_line_total", 0)),
            money(_value(item, "line_profit", 0)),
        ])
    while len(rows) < 5:
        rows.append(["", "", "", "", "", "", ""])
    table = Table(rows, colWidths=[0.38*inch,2.22*inch,1.0*inch,1.0*inch,0.92*inch,0.92*inch,1.0*inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), SOFT),
        ("TEXTCOLOR", (0,0), (-1,0), INK),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 7),
        ("ALIGN", (0,0), (0,-1), "CENTER"),
        ("ALIGN", (4,1), (-1,-1), "RIGHT"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,1), (-1,-1), 7.2),
        ("GRID", (0,0), (-1,-1), 0.45, LINE),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, colors.HexColor("#FAFBFD")]),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("MINROWHEIGHT", (0,1), (-1,-1), 29),
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
    inner = Table([[jmm, zelle]], colWidths=[2.65*inch,1.65*inch])
    inner.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LINEBEFORE", (1,0), (1,0), 0.5, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
    ]))
    box = Table([
        [Paragraph("PAYMENT INFORMATION", ParagraphStyle(
            "PaymentHeader", parent=s["label"], fontSize=9.5, textColor=NAVY
        ))],
        [inner],
    ], colWidths=[4.52*inch])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), PALE_BLUE),
        ("BOX", (0,0), (-1,-1), 0.65, LINE),
        ("LINEBELOW", (0,0), (-1,0), 0.65, LINE),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    return box


def _totals_box(quote, internal: bool):
    s = _styles()
    shipping = float(
        _value(quote, "shipping_total", 0) or 0
    )

    if internal:
        supplier_parts = (
            float(_value(quote, "supplier_total", 0) or 0)
            - shipping
        )

        rows = [
            ["Supplier Parts", money(supplier_parts)],
            ["Shipping", money(shipping)],
            [
                "Supplier Total",
                money(_value(quote, "supplier_total", 0)),
            ],
            [
                "Customer Total",
                money(_value(quote, "customer_total", 0)),
            ],
            [
                "NET PROFIT",
                money(_value(quote, "profit_total", 0)),
            ],
        ]
    else:
        rows = [
            [
                "Subtotal",
                money(_value(quote, "parts_subtotal", 0)),
            ],
        ]

        if shipping > 0:
            rows.append([
                "Shipping",
                money(shipping),
            ])

        rows.append([
            "TOTAL",
            money(_value(quote, "customer_total", 0)),
        ])

    table = Table(
        rows,
        colWidths=[1.35 * inch, 1.57 * inch],
    )

    final_row = len(rows) - 1

    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, final_row - 1), 0.35, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, final_row), (-1, final_row), NAVY),
        ("TEXTCOLOR", (0, final_row), (-1, final_row), WHITE),
        (
            "FONTNAME",
            (0, final_row),
            (-1, final_row),
            "Helvetica-Bold",
        ),
        ("FONTSIZE", (0, final_row), (-1, final_row), 11),
        ("TOPPADDING", (0, final_row), (-1, final_row), 7),
        ("BOTTOMPADDING", (0, final_row), (-1, final_row), 7),
    ]))

    return table

def _bottom_blocks(quote, internal):
    payment = _payment_box()
    totals = _totals_box(quote, internal)
    table = Table([[payment, totals]], colWidths=[4.52*inch,2.92*inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 0),
        ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ("LEFTPADDING", (1,0), (1,0), 8),
    ]))
    return table


def _footer_blocks():
    s = _styles()
    left = [
        Paragraph("PARTS IDENTIFICATION", s["footer_heading"]),
        Spacer(1, 4),
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
        Spacer(1, 4),
        Paragraph(
            "Manufacturer warranty applies where provided by the original supplier.",
            s["footer"],
        ),
    ]
    table = Table([[left, center, right]], colWidths=[2.48*inch,2.48*inch,2.48*inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LINEABOVE", (0,0), (-1,0), 0.8, NAVY),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    return table


def _quote_notice(internal):
    s = _styles()

    if internal:
        return Table(
            [[Paragraph(
                (
                    "INTERNAL USE ONLY - Supplier pricing "
                    "and profit are confidential."
                ),
                ParagraphStyle(
                    "InternalNotice",
                    parent=s["small"],
                    alignment=TA_CENTER,
                    textColor=colors.HexColor("#9B2C2C"),
                    fontName="Helvetica-Bold",
                ),
            )]],
            colWidths=[7.44 * inch],
            style=[
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, -1),
                    colors.HexColor("#FFF1F1"),
                ),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ],
        )

    return Paragraph(
        "Quote valid for 30 days.",
        ParagraphStyle(
            "CustomerQuoteNotice",
            parent=s["small"],
            alignment=TA_CENTER,
            textColor=colors.HexColor("#5E6D7E"),
            fontSize=7.5,
        ),
    )

def build_quote_pdf(quote, items: Iterable, path: Path, internal: bool):
    items = list(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    title = "INTERNAL QUOTE" if internal else "QUOTE"
    doc = _document(path, quote, title)

    story = [
        _header(quote, internal),
        Spacer(1, 9),
        Table([[""]], colWidths=[7.44*inch], style=[
            ("LINEBELOW", (0,0), (-1,-1), 1.1, NAVY),
            ("TOPPADDING", (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]),
        Spacer(1, 10),
        _information(quote) if internal else _customer_information(quote),
        Spacer(1, 10),
        _section_bar("QUOTED ITEMS" if not internal else "INTERNAL COST & PROFIT DETAIL"),
        _internal_items(items) if internal else _customer_items(items),
        Spacer(1, 10),
        _quote_notice(internal),
        Spacer(1, 10),
        _bottom_blocks(quote, internal),
        Spacer(1, 4),
        _footer_blocks(),
    ]
    doc.build(story)


def generate_quote_pdfs(quote, items: Iterable) -> dict[str, str]:
    items = list(items)
    paths = quote_paths(_value(quote, "customer"), _value(quote, "quote_number"))
    build_quote_pdf(quote, items, paths["customer"], internal=False)
    build_quote_pdf(quote, items, paths["internal"], internal=True)
    return {key: str(value) for key, value in paths.items()}
