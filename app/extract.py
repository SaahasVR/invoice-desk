"""Invoice extraction: text layer -> OCR for scans -> rule-based parsing -> optional LLM fallback.

Every extracted field carries a confidence and a source ("rules", "ocr", "derived", "llm") so
downstream checks (and the human reviewer) know how much to trust it.
"""
import base64
import io
import json
import os
import re
from datetime import datetime

import pdfplumber

from . import config

MONEY = r"\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --------------------------------------------------------------------------- reading
def read_document(path):
    """Return dict(text, method, ocr_confidence, pages). method is 'text' or 'ocr'."""
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(text.strip()) >= 40:
        return {"text": text, "method": "text", "ocr_confidence": None, "pages": pages}
    return _ocr(path, pages)


OCR_DPI = 200            # 300 was needlessly slow on small hosts; 200 reads these invoices just as well
OCR_TIMEOUT_SEC = 40     # per page: a stuck OCR must route to a person, never hang the run


def _ocr(path, pages):
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except Exception as e:  # pragma: no cover
        return {"text": "", "method": "ocr_unavailable", "ocr_confidence": 0.0, "pages": pages, "error": str(e)}
    chunks, confs = [], []
    try:
        for img in convert_from_path(path, dpi=OCR_DPI):
            gray = img.convert("L")
            # ONE OCR pass: rebuild the text lines and the confidence from the same result
            data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT, config="--psm 6",
                                             timeout=OCR_TIMEOUT_SEC)
            lines, order = {}, []
            for i, word in enumerate(data["text"]):
                if not str(word).strip():
                    continue
                key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                if key not in lines:
                    lines[key] = []
                    order.append(key)
                lines[key].append(str(word))
                if float(data["conf"][i]) >= 0:
                    confs.append(float(data["conf"][i]))
            chunks.append("\n".join(" ".join(lines[k]) for k in order))
    except Exception as e:  # tesseract/poppler missing, or OCR timed out: degrade to human review, don't crash or hang
        return {"text": "", "method": "ocr_unavailable", "ocr_confidence": 0.0, "pages": pages, "error": str(e)[:200]}
    conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return {"text": "\n".join(chunks), "method": "ocr", "ocr_confidence": round(conf, 3), "pages": pages}


def first_page_png_b64(path):
    from pdf2image import convert_from_path
    img = convert_from_path(path, dpi=150, first_page=1, last_page=1)[0]
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- parsing
def _num(s):
    try:
        return round(float(s.replace(",", "")), 2)
    except Exception:
        return None


def _norm_date(s):
    s = s.strip().replace(",", "")
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d %b %Y", "%b %d %Y", "%d %B %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


DATE_PAT = r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"


def parse_fields(text, base_conf=0.95, source="rules"):
    """Rule-based parser tolerant of several invoice layouts. Returns {field: {value, confidence, source}}."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
    f = {}

    def put(name, value, conf=None, src=None):
        if value is None or value == "":
            return
        f[name] = {"value": value, "confidence": round(conf if conf is not None else base_conf, 2), "source": src or source}

    # vendor: first line, minus a trailing "INVOICE"/"TAX INVOICE" title that shares the line
    if lines:
        v = re.sub(r"\s*((TAX\s+)?INVOICE|CREDIT\s+(NOTE|MEMO))\s*$", "", lines[0], flags=re.I).strip()
        put("vendor_name", v, base_conf - 0.05)

    joined = "\n".join(lines)

    # document type: a credit note must never be treated as a payable invoice
    if re.search(r"\bCREDIT\s+(NOTE|MEMO)\b", "\n".join(lines[:12]), re.I):
        put("doc_type", "credit_note", 0.95)
        m = re.search(r"(?:Original\s+Invoice|Against\s+Invoice|Credit\s+for\s+Invoice|Applies\s+to\s+Invoice)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", joined, re.I)
        if m:
            put("original_invoice", m.group(1).strip(".,"), 0.9)

    m = re.search(r"(?:Credit\s+(?:Note|Memo)\s*(?:No\.?|Number|#)|Tax\s+Invoice\s+Number|(?<!Original )(?<!Against )Invoice\s*(?:No\.?|Number|#)|INVOICE\s*#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,})", joined, re.I)
    if m and m.group(1).lower() not in ("date", "number", "total"):
        put("invoice_number", m.group(1).strip(".,"))

    m = re.search(r"(?:Invoice\s*Date|Issued\s*on|Date\s*of\s*Issue|Date)\s*[:\-]?\s*" + DATE_PAT, joined, re.I)
    if m:
        d = _norm_date(m.group(1))
        if d:
            put("invoice_date", d)

    m = re.search(r"(?:PO\s*Number|PO\s*No\.?|Your\s*Ref(?:erence)?|Purchase\s*Order|PO\s*#|P\.O\.)\s*[:#]?\s*([A-Za-z]{0,3}-?\d{3,})", joined, re.I)
    if m:
        put("po_reference", m.group(1).upper())
    else:
        m = re.search(r"\b(PO-\d{3,})\b", joined, re.I)
        if m:
            put("po_reference", m.group(1).upper(), 0.65)

    cur = re.search(r"Currency\s*:\s*([A-Z]{3})", joined)
    put("currency", cur.group(1) if cur else "USD", 0.95 if cur else 0.6, source if cur else "assumed")

    # amounts: last money value on the labelled line
    def amount_for(label_regex):
        hits = []
        for l in lines:
            if re.search(label_regex, l, re.I):
                ms = re.findall(MONEY, l)
                if ms:
                    hits.append(_num(ms[-1]))
        return hits[-1] if hits else None

    total = amount_for(r"^(?:.*\s)?(?:Grand\s+Total|Total\s+Due|Amount\s+Due|Total)(?:\s*\(incl\.? tax\))?\b(?!.*sub)")
    # avoid picking Subtotal as Total
    for l in lines:
        if re.match(r"^(?:Grand\s+Total|Total\s+Due|Amount\s+Due|Total)\b", l, re.I):
            ms = re.findall(MONEY, l)
            if ms:
                total = _num(ms[-1])
    subtotal = None
    for l in lines:
        if re.match(r"^(?:Subtotal|Sub-total|Net\s+amount)\b", l, re.I):
            ms = re.findall(MONEY, l)
            if ms:
                subtotal = _num(ms[-1])
    tax = None
    for l in lines:
        if re.match(r"^(?:Sales\s+Tax|VAT|GST|Tax)\b", l, re.I):
            ms = re.findall(MONEY, l)
            if ms:
                tax = _num(ms[-1])
    embedded = re.search(r"Includes\s+([\d.]+)%\s+tax\s+of\s+" + MONEY, joined, re.I)
    if embedded and tax is None:
        tax = _num(embedded.group(2))
        put("tax_embedded", True, 0.9)
    put("subtotal", subtotal)
    put("tax", tax)
    put("total", total)

    items = []
    for l in lines:
        m = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})$", l)
        if m and not re.match(r"^(Subtotal|Total|Tax|VAT|GST)", l, re.I):
            items.append({"description": m.group(1), "qty": float(m.group(2)),
                          "unit_price": _num(m.group(3)), "amount": _num(m.group(4))})
    if items:
        put("line_items", items, base_conf - 0.05)

    # derive missing totals so arithmetic can still be checked (lower confidence, flagged as derived)
    if "total" not in f:
        if "subtotal" in f and "tax" in f:
            put("total", round(f["subtotal"]["value"] + f["tax"]["value"], 2), 0.55, "derived")
        elif items:
            put("total", round(sum(i["amount"] for i in items), 2), 0.50, "derived")
    return f


def scale_for_ocr(fields, ocr_conf):
    """Scanned text is less trustworthy: scale confidences by the OCR engine's own confidence."""
    if ocr_conf is None:
        return fields
    k = max(0.3, min(1.0, ocr_conf + 0.1))
    for v in fields.values():
        v["confidence"] = round(v["confidence"] * k, 2)
        if v["source"] == "rules":
            v["source"] = "ocr"
    return fields


# --------------------------------------------------------------------------- optional LLM fallback
REQUIRED = ["vendor_name", "invoice_number", "invoice_date", "total"]

LLM_PROMPT = """You are an accounts-payable extraction engine. Read this vendor invoice and return ONLY a JSON object with keys:
vendor_name, invoice_number, invoice_date (YYYY-MM-DD), po_reference, currency, subtotal, tax, total (numbers, no symbols),
line_items (list of {description, qty, unit_price, amount}). Use null for anything not present on the document - never guess."""


def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def llm_extract(text, pdf_path=None, scanned=False):
    """Ask Claude to fill gaps. Returns (fields, note). Never raises: failures degrade to rules-only."""
    if not llm_available():
        return {}, "LLM fallback skipped (no ANTHROPIC_API_KEY set)."
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=30.0, max_retries=1)  # an unreachable API must never hang an invoice
        content = [{"type": "text", "text": LLM_PROMPT + "\n\nDocument text:\n" + (text or "(none - see image)")}]
        if scanned and pdf_path:
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": first_page_png_b64(pdf_path)}})
        resp = client.messages.create(model=config.ANTHROPIC_MODEL, max_tokens=1200,
                                      messages=[{"role": "user", "content": content}])
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
        data = json.loads(raw)
        out = {}
        for k, v in data.items():
            if v not in (None, "", []):
                out[k] = {"value": v, "confidence": 0.80, "source": "llm"}
        return out, f"LLM ({config.ANTHROPIC_MODEL}) returned {len(out)} fields."
    except Exception as e:
        return {}, f"LLM fallback failed and was ignored: {type(e).__name__}: {e}"[:300]


def needs_fallback(fields):
    """Fallback only when rules left gaps/low confidence in required fields - keeps cost and variance down."""
    for k in REQUIRED:
        if k not in fields or fields[k]["confidence"] < config.MIN_FIELD_CONFIDENCE:
            return True
    return False


def merge(rule_fields, llm_fields):
    """LLM only fills gaps or replaces low-confidence fields; confident rule output is never overridden."""
    out = dict(rule_fields)
    filled = []
    for k, v in llm_fields.items():
        if k not in out or out[k]["confidence"] < config.MIN_FIELD_CONFIDENCE:
            out[k] = v
            filled.append(k)
    return out, filled


def _as_float(v):
    try:
        return round(float(str(v).replace(",", "").replace("$", "")), 2)
    except (TypeError, ValueError):
        return None


def _alnum(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def compare(rule_fields, llm_fields):
    """Cross-check: where BOTH the rules (confidently) and the LLM produced a value, do they agree?
    Returns (agreed_field_names, [(field, rules_value, llm_value)])."""
    import difflib
    agreed, diffs = [], []
    for k in ["invoice_number", "invoice_date", "po_reference", "subtotal", "tax", "total", "vendor_name"]:
        r, l = rule_fields.get(k), llm_fields.get(k)
        if not r or not l or r["confidence"] < config.MIN_FIELD_CONFIDENCE:
            continue
        rv, lv = r["value"], l["value"]
        if k in ("subtotal", "tax", "total"):
            same = _as_float(rv) is not None and _as_float(lv) is not None and abs(_as_float(rv) - _as_float(lv)) < 0.01
        elif k == "vendor_name":
            same = difflib.SequenceMatcher(None, _alnum(rv), _alnum(lv)).ratio() >= 0.85
        elif k == "invoice_date":
            same = str(rv).strip() == str(lv).strip()
        else:
            same = _alnum(rv) == _alnum(lv)
        (agreed if same else diffs).append(k if same else (k, rv, lv))
    return agreed, diffs
