from __future__ import annotations

from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ROOT = PROJECT_ROOT / "documents" / "Customers"


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


def _styles():
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle("brand", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=19, textColor=colors.HexColor("#0B2F57")),
        "title": ParagraphStyle("title", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=22, alignment=TA_RIGHT, textColor=colors.HexColor("#0B63CE")),
        "normal": ParagraphStyle("normal2", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=12, textColor=colors.HexColor("#1F2937")),
        "small": ParagraphStyle("small2", parent=base["Normal"], fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#52657A")),
        "label": ParagraphStyle("label2", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8, textColor=colors.HexColor("#52657A")),
    }


def _footer(canvas, doc, title: str, number: str):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D9E2EC"))
    canvas.line(0.55 * inch, 0.52 * inch, 7.95 * inch, 0.52 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#5F6D82"))
    canvas.drawString(0.58 * inch, 0.33 * inch, "PartsLink Global - Worldwide Parts Sourcing & Logistics")
    canvas.drawRightString(7.92 * inch, 0.33 * inch, f"{title} {number} | Page {doc.page}")
    canvas.restoreState()


def _build(path: Path, quote, items, internal: bool):
    styles = _styles()
    title = "INTERNAL QUOTE" if internal else "QUOTE"
    doc = BaseDocTemplate(str(path), pagesize=LETTER, leftMargin=.55*inch, rightMargin=.55*inch, topMargin=.55*inch, bottomMargin=.72*inch)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="quote", frames=[frame], onPage=lambda c, d: _footer(c, d, title, quote["quote_number"]))])

    header = Table([[
        [Paragraph("PartsLink Global", styles["brand"]), Paragraph("Worldwide Parts Sourcing & Logistics", styles["small"])],
        [Paragraph(title, styles["title"]), Paragraph(f"<b>{quote['quote_number']}</b><br/>{quote['quote_date']}<br/>{quote['status']}", ParagraphStyle("meta", parent=styles["small"], alignment=TA_RIGHT))]
    ]], colWidths=[4.5*inch, 2.4*inch])
    header.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 0), ("RIGHTPADDING", (0,0), (-1,-1), 0)]))

    equipment = " ".join(x for x in [quote["manufacturer"], quote["machine"]] if x) or "Not Provided"
    info = Table([[
        [Paragraph("BILL TO", styles["label"]), Paragraph(str(quote["customer"]), styles["normal"]), Paragraph(str(quote["company"] or ""), styles["small"])],
        [Paragraph("MACHINE / VEHICLE", styles["label"]), Paragraph(equipment, styles["normal"]), Paragraph("VIN / PIN / SERIAL", styles["label"]), Paragraph(str(quote["pin_serial"] or "Not Provided"), styles["normal"])]
    ]], colWidths=[3.45*inch, 3.45*inch])
    info.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#F6F9FC")), ("BOX", (0,0), (-1,-1), .6, colors.HexColor("#D6E0EA")), ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 10), ("RIGHTPADDING", (0,0), (-1,-1), 10), ("TOPPADDING", (0,0), (-1,-1), 9), ("BOTTOMPADDING", (0,0), (-1,-1), 9)]))

    if internal:
        data = [["Qty", "Description", "Vendor", "Part #", "Cost", "Sell", "Profit"]]
        for item in items:
            data.append([item["quantity"], Paragraph(str(item["description"]), styles["small"]), str(item["supplier_name"] or ""), str(item["supplier_part_number"] or ""), money(item["supplier_line_total"]), money(item["customer_line_total"]), money(item["line_profit"])])
        widths = [.38*inch, 2.35*inch, 1.05*inch, 1.0*inch, .72*inch, .72*inch, .72*inch]
    else:
        data = [["Qty", "Description", "Unit Price", "Line Total"]]
        for item in items:
            data.append([item["quantity"], Paragraph(str(item["description"]), styles["normal"]), money(item["customer_unit_price"]), money(item["customer_line_total"])])
        widths = [.55*inch, 4.35*inch, 1.0*inch, 1.0*inch]

    item_table = Table(data, colWidths=widths, repeatRows=1)
    item_table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B2F57")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTNAME", (0,1), (-1,-1), "Helvetica"), ("FONTSIZE", (0,0), (-1,-1), 8), ("GRID", (0,0), (-1,-1), .4, colors.HexColor("#D6E0EA")), ("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("ALIGN", (-3,1), (-1,-1), "RIGHT"), ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F8FAFC")]), ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))

    if internal:
        totals = [["Vendor Parts", money(float(quote["supplier_total"]) - float(quote["shipping_total"]))], ["Shipping", money(quote["shipping_total"])], ["Vendor Total", money(quote["supplier_total"])], ["Customer Total", money(quote["customer_total"])], ["Net Profit", money(quote["profit_total"])]]
    else:
        totals = [["Parts Subtotal", money(quote["parts_subtotal"])], ["Shipping", money(quote["shipping_total"])], ["Quote Total", money(quote["customer_total"])]]
    totals_table = Table(totals, colWidths=[1.55*inch, 1.15*inch], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([("FONTNAME", (0,0), (0,-1), "Helvetica"), ("FONTNAME", (1,0), (1,-1), "Helvetica-Bold"), ("ALIGN", (1,0), (1,-1), "RIGHT"), ("FONTSIZE", (0,0), (-1,-1), 9), ("LINEABOVE", (0,-1), (-1,-1), 1, colors.HexColor("#0B63CE")), ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#EDF5FF")), ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))

    terms = Paragraph("Parts are supplied based on the vehicle or equipment information provided by the customer. Please verify compatibility before installation.<br/><br/>Manufacturer warranty applies where provided by the original supplier.<br/><br/>Pricing and availability are subject to change until confirmed. Quote valid for 30 days.", styles["small"])
    doc.build([header, Spacer(1,14), info, Spacer(1,14), item_table, Spacer(1,12), totals_table, Spacer(1,16), terms])


def generate_quote_pdfs(quote, items):
    items = list(items)
    paths = quote_paths(quote["customer"], quote["quote_number"])
    _build(paths["customer"], quote, items, False)
    _build(paths["internal"], quote, items, True)
    return {key: str(value) for key, value in paths.items()}
