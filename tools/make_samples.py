"""Generate realistic test invoices (PDF) and a manifest describing the expected outcome of each.

Three layouts mimic different vendor styles so extraction can't rely on one template:
  A  "Invoice No / PO Number / Subtotal / Tax / Total"
  B  "INVOICE # / Your Ref / Amount Due" (tax shown as a single line)
  C  "TAX INVOICE / Purchase Order / Grand Total (incl. tax)" (tax embedded, no separate line)
"""
import json
import os
import random
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
os.makedirs(OUT, exist_ok=True)

VENDORS = {
    "V001": ("Northwind Supplies LLC", "410 Harbor Way, Portland, OR 97204", "A"),
    "V002": ("Brightwave Digital Ltd", "22 Canal Street, Austin, TX 78701", "B"),
    "V003": ("Apex Industrial Parts Inc", "9100 Foundry Road, Detroit, MI 48201", "C"),
    "V004": ("Meridian Logistics Co", "77 Dockside Ave, Savannah, GA 31401", "A"),
    "VX": ("Quickfix Services Co", "5 Elm Court, Reno, NV 89501", "B"),
}
BILL_TO = ("Orbital Foods Inc.", "1200 Market Street, Suite 400, San Francisco, CA 94102")


def money(v):
    return f"${v:,.2f}"


def draw(path, vendor_key, inv_no, date, po_ref, lines, tax_rate, tax_mode="separate", total_override=None, note=None):
    name, addr, tpl = VENDORS[vendor_key]
    subtotal = round(sum(q * u for _, q, u in lines), 2)
    tax = round(subtotal * tax_rate, 2)
    total = round(subtotal + tax, 2) if total_override is None else total_override
    c = canvas.Canvas(path, pagesize=letter)
    w, h = letter
    accent = {"A": colors.HexColor("#1d4ed8"), "B": colors.HexColor("#0f766e"), "C": colors.HexColor("#9a3412")}[tpl]

    c.setFillColor(accent)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, h - 60, name)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(50, h - 76, addr)

    title = {"A": "INVOICE", "B": "INVOICE", "C": "TAX INVOICE"}[tpl]
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 50, h - 60, title)
    c.setFont("Helvetica", 10)
    y = h - 90
    if tpl == "A":
        if inv_no:
            c.drawRightString(w - 50, y, f"Invoice No: {inv_no}")
            y -= 14
        if date:
            c.drawRightString(w - 50, y, f"Invoice Date: {date}")
            y -= 14
        if po_ref:
            c.drawRightString(w - 50, y, f"PO Number: {po_ref}")
            y -= 14
        c.drawRightString(w - 50, y, "Terms: Net 30")
    elif tpl == "B":
        if inv_no:
            c.drawRightString(w - 50, y, f"INVOICE # {inv_no}")
            y -= 14
        if date:
            c.drawRightString(w - 50, y, f"Issued on {date}")
            y -= 14
        if po_ref:
            c.drawRightString(w - 50, y, f"Your Ref: {po_ref}")
            y -= 14
        c.drawRightString(w - 50, y, "Payment due within 30 days")
    else:
        if inv_no:
            c.drawRightString(w - 50, y, f"Tax Invoice Number: {inv_no}")
            y -= 14
        if date:
            c.drawRightString(w - 50, y, f"Date of Issue: {date}")
            y -= 14
        if po_ref:
            c.drawRightString(w - 50, y, f"Purchase Order: {po_ref}")
            y -= 14
        c.drawRightString(w - 50, y, "Currency: USD")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, h - 130, "BILL TO")
    c.setFont("Helvetica", 10)
    c.drawString(50, h - 144, BILL_TO[0])
    c.drawString(50, h - 158, BILL_TO[1])

    ty = h - 205
    c.setFillColor(accent)
    c.rect(50, ty, w - 100, 20, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(58, ty + 6, "Description")
    c.drawRightString(w - 210, ty + 6, "Qty")
    c.drawRightString(w - 130, ty + 6, "Unit price")
    c.drawRightString(w - 58, ty + 6, "Amount")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    ty -= 18
    for desc, q, u in lines:
        c.drawString(58, ty, desc)
        c.drawRightString(w - 210, ty, f"{q:g}")
        c.drawRightString(w - 130, ty, money(u))
        c.drawRightString(w - 58, ty, money(q * u))
        ty -= 18
    c.line(50, ty + 8, w - 50, ty + 8)
    ty -= 16

    def row(label, val, bold=False):
        nonlocal ty
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(w - 130, ty, label)
        c.drawRightString(w - 58, ty, val)
        ty -= 18

    if tpl == "A":
        row("Subtotal", money(subtotal))
        row(f"Sales Tax ({tax_rate*100:g}%)", money(tax))
        row("Total", money(total), True)
    elif tpl == "B":
        row("Subtotal", money(subtotal))
        if tax_rate:
            row(f"VAT {tax_rate*100:g}%", money(tax))
        row("Amount Due", money(total), True)
    else:
        if tax_mode == "embedded":
            row("Grand Total (incl. tax)", money(total), True)
            c.setFont("Helvetica-Oblique", 8)
            c.drawRightString(w - 58, ty + 4, f"Includes {tax_rate*100:g}% tax of {money(round(total - total/(1+tax_rate), 2))}")
        else:
            row("Net amount", money(subtotal))
            row(f"GST {tax_rate*100:g}%", money(tax))
            row("Grand Total", money(total), True)
    if note:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(50, 120, note)
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(50, 60, "Remit to: see account details on file. Thank you for your business.")
    c.save()
    return subtotal, tax, total


def make_scanned(src_pdf, dst_pdf):
    """Rasterise a PDF page, add noise and a slight rotation, write back as an image-only PDF (no text layer)."""
    from pdf2image import convert_from_path
    from PIL import Image, ImageFilter

    page = convert_from_path(src_pdf, dpi=170)[0].convert("L")
    page = page.rotate(0.8, expand=True, fillcolor=255)
    random.seed(7)
    px = page.load()
    for _ in range(int(page.width * page.height * 0.002)):
        x, y = random.randrange(page.width), random.randrange(page.height)
        px[x, y] = random.choice([60, 110, 200])
    page = page.filter(ImageFilter.GaussianBlur(0.7))
    page.convert("RGB").save(dst_pdf, "PDF", resolution=170)


def draw_multipage(path, vendor_key, inv_no, date, po_ref, target_subtotal, tax_rate, n_lines=30, per_page=17):
    """Invoice whose line items spill onto a second page; totals only appear on the last page."""
    name, addr, _ = VENDORS[vendor_key]
    base = [("Facilities consumable item %02d" % (i + 1), 1 + (i % 4), round(8.50 + (i * 3.35) % 41, 2)) for i in range(n_lines - 1)]
    cur = round(sum(q * u for _, q, u in base), 2)
    lines = base + [("Delivery and handling (quarter)", 1, round(target_subtotal - cur, 2))]
    assert lines[-1][2] > 0, "target subtotal too small for the generated lines"
    subtotal = round(sum(q * u for _, q, u in lines), 2)
    tax = round(subtotal * tax_rate, 2)
    total = round(subtotal + tax, 2)
    c = canvas.Canvas(path, pagesize=letter)
    w, h = letter
    pages = (len(lines) + per_page - 1) // per_page
    accent = colors.HexColor("#1d4ed8")
    for pg in range(pages):
        c.setFillColor(accent); c.setFont("Helvetica-Bold", 20); c.drawString(50, h - 60, name)
        c.setFillColor(colors.black); c.setFont("Helvetica", 9); c.drawString(50, h - 76, addr)
        c.setFont("Helvetica-Bold", 22); c.drawRightString(w - 50, h - 60, "INVOICE")
        c.setFont("Helvetica", 10)
        y = h - 90
        for t in ([f"Invoice No: {inv_no}", f"Invoice Date: {date}", f"PO Number: {po_ref}", "Terms: Net 30"] if pg == 0 else [f"Invoice No: {inv_no}  (continued)"]):
            c.drawRightString(w - 50, y, t); y -= 14
        ty = h - 150 if pg == 0 else h - 120
        if pg == 0:
            c.setFont("Helvetica-Bold", 10); c.drawString(50, h - 130, "BILL TO"); c.setFont("Helvetica", 10)
            c.drawString(50, h - 144, BILL_TO[0]); c.drawString(50, h - 158, BILL_TO[1]); ty = h - 195
        c.setFillColor(accent); c.rect(50, ty, w - 100, 20, fill=1, stroke=0); c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(58, ty + 6, "Description"); c.drawRightString(w - 210, ty + 6, "Qty")
        c.drawRightString(w - 130, ty + 6, "Unit price"); c.drawRightString(w - 58, ty + 6, "Amount")
        c.setFillColor(colors.black); c.setFont("Helvetica", 10); ty -= 18
        for desc, q, u in lines[pg * per_page:(pg + 1) * per_page]:
            c.drawString(58, ty, desc); c.drawRightString(w - 210, ty, f"{q:g}")
            c.drawRightString(w - 130, ty, money(u)); c.drawRightString(w - 58, ty, money(q * u)); ty -= 18
        if pg == pages - 1:
            c.line(50, ty + 8, w - 50, ty + 8); ty -= 16
            for label, val, bold in (("Subtotal", money(subtotal), False), (f"Sales Tax ({tax_rate*100:g}%)", money(tax), False), ("Total", money(total), True)):
                c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
                c.drawRightString(w - 130, ty, label); c.drawRightString(w - 58, ty, val); ty -= 18
        c.setFont("Helvetica", 8); c.setFillColor(colors.grey)
        c.drawString(50, 50, f"Page {pg + 1} of {pages}")
        c.setFillColor(colors.black)
        c.showPage()
    c.save()
    return subtotal, tax, total


def draw_credit_note(path, vendor_key, cn_no, date, original_inv, po_ref, items, tax_rate):
    name, addr, _ = VENDORS[vendor_key]
    subtotal = round(sum(q * u for _, q, u in items), 2)
    tax = round(subtotal * tax_rate, 2)
    total = round(subtotal + tax, 2)
    c = canvas.Canvas(path, pagesize=letter)
    w, h = letter
    accent = colors.HexColor("#1d4ed8")
    c.setFillColor(accent); c.setFont("Helvetica-Bold", 20); c.drawString(50, h - 60, name)
    c.setFillColor(colors.black); c.setFont("Helvetica", 9); c.drawString(50, h - 76, addr)
    c.setFont("Helvetica-Bold", 22); c.drawRightString(w - 50, h - 60, "CREDIT NOTE")
    c.setFont("Helvetica", 10)
    y = h - 90
    for t in (f"Credit Note No: {cn_no}", f"Credit Date: {date}", f"Original Invoice: {original_inv}", f"PO Number: {po_ref}", "Reason: goods returned"):
        c.drawRightString(w - 50, y, t); y -= 14
    c.setFont("Helvetica-Bold", 10); c.drawString(50, h - 150, "CREDITED TO"); c.setFont("Helvetica", 10)
    c.drawString(50, h - 164, BILL_TO[0]); c.drawString(50, h - 178, BILL_TO[1])
    ty = h - 225
    c.setFillColor(accent); c.rect(50, ty, w - 100, 20, fill=1, stroke=0); c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 10)
    c.drawString(58, ty + 6, "Description"); c.drawRightString(w - 210, ty + 6, "Qty")
    c.drawRightString(w - 130, ty + 6, "Unit price"); c.drawRightString(w - 58, ty + 6, "Amount")
    c.setFillColor(colors.black); c.setFont("Helvetica", 10); ty -= 18
    for desc, q, u in items:
        c.drawString(58, ty, desc); c.drawRightString(w - 210, ty, f"{q:g}")
        c.drawRightString(w - 130, ty, money(u)); c.drawRightString(w - 58, ty, "-" + money(q * u)); ty -= 18
    c.line(50, ty + 8, w - 50, ty + 8); ty -= 16
    for label, val, bold in (("Subtotal", "-" + money(subtotal), False), (f"Sales Tax ({tax_rate*100:g}%)", "-" + money(tax), False), ("Total credit", "-" + money(total), True)):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(w - 130, ty, label); c.drawRightString(w - 58, ty, val); ty -= 18
    c.save()
    return subtotal, tax, total


def make_rough_scan(src_pdf, dst_pdf):
    """Simulate a phone photo of a printed invoice: tilt, uneven lighting, blur, low resolution, heavy JPEG."""
    import io
    from pdf2image import convert_from_path
    from PIL import Image, ImageFilter, ImageEnhance

    page = convert_from_path(src_pdf, dpi=110)[0].convert("L")
    random.seed(11)
    w, h = page.size
    # uneven lighting: a diagonal shadow gradient plus a darker corner
    shade = Image.linear_gradient("L").resize((w, h)).rotate(35, expand=False, fillcolor=255)
    page = Image.blend(page, ImageEnhance.Brightness(shade).enhance(0.85), 0.28)
    page = page.rotate(2.6, expand=True, fillcolor=205, resample=Image.BICUBIC)
    page = ImageEnhance.Contrast(page).enhance(0.8)
    px = page.load()
    for _ in range(int(page.width * page.height * 0.012)):
        x, y = random.randrange(page.width), random.randrange(page.height)
        px[x, y] = max(0, min(255, px[x, y] + random.choice([-90, -60, 50, 80])))
    page = page.filter(ImageFilter.GaussianBlur(1.35))
    buf = io.BytesIO()
    page.convert("RGB").save(buf, "JPEG", quality=30)
    buf.seek(0)
    Image.open(buf).convert("RGB").save(dst_pdf, "PDF", resolution=110)


def main():
    S = []

    def lines_to(target, base):
        """Return lines whose subtotal equals target (adjust the last line)."""
        cur = sum(q * u for _, q, u in base[:-1])
        d, q, _ = base[-1]
        return base[:-1] + [(d, q, round((target - cur) / q, 2))]

    def add2(fid, title, tag, desc, expected, vendor_key, inv_no, date, po_ref, target_subtotal, base_lines, tax_rate,
             tax_mode="separate", total_override=None, note=None, scanned=False):
        path = os.path.join(OUT, f"{fid}.pdf")
        lines = lines_to(target_subtotal, base_lines)
        # make sure the last line's unit price is clean to 2dp and total is exact
        sub = round(sum(q * u for _, q, u in lines), 2)
        if abs(sub - target_subtotal) > 0.005:
            lines[-1] = (lines[-1][0], 1, round(target_subtotal - sum(q * u for _, q, u in lines[:-1]), 2))
        tmp = path if not scanned else path.replace(".pdf", "_text.pdf")
        draw(tmp, vendor_key, inv_no, date, po_ref, lines, tax_rate, tax_mode, total_override, note)
        if scanned:
            make_scanned(tmp, path)
            os.remove(tmp)
        S.append({"id": fid, "file": f"{fid}.pdf", "title": title, "tag": tag, "description": desc, "expected": expected})

    add2("01_happy_path", "Clean invoice, exact PO match", "happy",
         "Northwind bills PO-1001 for exactly the agreed amount. Vendor approved, PO open, no duplicates.", "APPROVED",
         "V001", "NW-88214", "2026-10-20", "PO-1001", 4800.00,
         [("Copy paper, 10-ream cases", 24, 62.50), ("Ink cartridges, black", 50, 38.00), ("Desk organisers", 60, 12.00), ("Whiteboard markers and sundries", 1, 0)], 0.08)

    add2("02a_within_tolerance", "Slightly over PO, within tolerance", "edge",
         "Apex bills $68.75 over the PO value including tax (0.5%). Under the 2% / $100 auto-approve band, so it passes with a note.", "APPROVED",
         "V003", "APX-30517", "2026-10-22", "PO-1002", 12562.50,
         [("M8 hex bolts, zinc (box 500)", 120, 48.00), ("M10 flange nuts (box 250)", 80, 36.00), ("Washers, assorted and fittings (lot)", 1, 0)], 0.10, tax_mode="separate")

    add2("02b_over_tolerance", "Over PO beyond tolerance", "edge",
         "Apex bills 8% more than the PO value. Beyond auto-approve but under the dispute threshold, so a person must decide.", "NEEDS_REVIEW",
         "V003", "APX-30561", "2026-10-24", "PO-1005", 6480.00,
         [("Brass ball valves 1in", 30, 120.00), ("Pressure gauges", 10, 85.00), ("Expedite surcharge", 1, 0)], 0.10, tax_mode="separate")

    add2("03_duplicate_altered_number", "Duplicate with altered invoice number", "edge",
         "Meridian re-sends invoice INV-2041 as 'INV-2041A'. Same vendor, amount and date as one already paid.", "ON_HOLD",
         "V004", "INV-2041A", "2026-10-12", "PO-1003", 3200.00,
         [("Freight, Savannah to Atlanta (8 loads)", 8, 400.00)], 0.0)

    add2("04a_split_po_ok", "Split PO: second milestone invoice", "edge",
         "Brightwave bills $4,000 against PO-1004 ($12,000). $4,000 was already paid, so cumulative billing stays within the PO.", "APPROVED",
         "V002", "BW-0088", "2026-10-28", "PO-1004", 4000.00,
         [("Milestone 2: UI design system", 1, 0)], 0.0)

    add2("04b_split_po_overrun", "Split PO: invoice would overrun the PO", "edge",
         "Brightwave bills $4,800 against PO-1008 ($10,000). With $6,000 already paid, cumulative billing would reach $10,800, over the PO.", "NEEDS_REVIEW",
         "V002", "BW-0089", "2026-10-29", "PO-1008", 4800.00,
         [("Milestone 3: frontend build", 1, 0)], 0.0)

    add2("05_missing_fields", "Missing invoice number and date", "edge",
         "Apex sends an invoice with no invoice number and no date. The process can't dedupe or age it, so it asks for exactly what's missing.", "NEEDS_REVIEW",
         "V003", None, None, "PO-1005", 6000.00,
         [("Brass ball valves 1in", 30, 120.00), ("Pressure gauges", 30, 0)], 0.10)

    add2("06_scanned_image", "Scanned (image-only) invoice", "edge",
         "Meridian's invoice arrives as a photographed scan with no text layer. OCR recovers the fields, then the normal checks run.", "APPROVED",
         "V004", "ML-5530", "2026-10-26", "PO-1006", 1850.00,
         [("Pallet handling, inbound", 37, 40.00), ("Storage fee", 1, 0)], 0.0, scanned=True)

    add2("07_no_po_reference", "No PO reference on the invoice", "edge",
         "Northwind quotes no PO. The process infers the PO from vendor + amount but won't pay on an inferred match alone.", "NEEDS_REVIEW",
         "V001", "NW-88307", "2026-10-27", None, 960.00,
         [("Toner cartridge, colour set", 6, 160.00)], 0.08)

    add2("08_unapproved_vendor", "Vendor not in the approved master", "edge",
         "An unknown vendor submits an invoice quoting a real PO number. Quoting a PO is not proof of legitimacy.", "REJECTED",
         "VX", "QF-1001", "2026-10-25", "PO-1001", 4800.00,
         [("Consulting services", 12, 400.00)], 0.0)


    # ---- additional realism samples
    p = os.path.join(OUT, "09_multipage.pdf")
    draw_multipage(p, "V001", "NW-88455", "2026-10-29", "PO-1009", 3000.00, 0.08)
    S.append({"id": "09_multipage", "file": "09_multipage.pdf", "title": "Two-page invoice, totals on page 2", "tag": "edge",
              "description": "Northwind's invoice runs to two pages with 30 line items; the subtotal, tax and total only appear on page 2. All pages must be read and reconciled.",
              "expected": "APPROVED"})

    p = os.path.join(OUT, "10_credit_note.pdf")
    draw_credit_note(p, "V001", "CN-4410", "2026-10-30", "NW-88214", "PO-1001", [("Returned desk organisers", 20, 12.00), ("Returned ink cartridges", 2, 40.00)], 0.08)
    S.append({"id": "10_credit_note", "file": "10_credit_note.pdf", "title": "Credit note, not an invoice", "tag": "edge",
              "description": "Northwind sends a credit note for returned goods. It must never be paid, and it is linked to the invoice it credits.",
              "expected": "NEEDS_REVIEW"})

    p = os.path.join(OUT, "11_rough_scan.pdf")
    tmp = p.replace(".pdf", "_text.pdf")
    lines = lines_to(2460.00, [("Cross-dock labour, week 44 (hours)", 82, 28.00), ("Equipment rental", 1, 0)])
    draw(tmp, "V004", "ML-5611", "2026-10-30", "PO-1010", lines, 0.0)
    make_rough_scan(tmp, p)
    os.remove(tmp)
    S.append({"id": "11_rough_scan", "file": "11_rough_scan.pdf", "title": "Poor phone photo of an invoice", "tag": "edge",
              "description": "Meridian's invoice is a tilted, shadowed, low-resolution phone photo. If the scan is too poor to trust, a person confirms the values.",
              "expected": "NEEDS_REVIEW"})

    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump(S, f, indent=2)
    print(f"wrote {len(S)} invoices to {OUT}")


if __name__ == "__main__":
    main()
