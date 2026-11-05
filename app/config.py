"""Business rules and thresholds. Every number here is an assumption the AP team can tune.

The values below are DEFAULTS. The ones in DEFAULT_SETTINGS can be changed from the Rules tab
(admin only) and are stored in the database.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SAMPLES_DIR = os.path.join(BASE_DIR, "samples")
DB_PATH = os.environ.get("APFLOW_DB", os.path.join(BASE_DIR, "runs.db"))

# Tunable from the UI (stored in the DB; these are the defaults)
DEFAULT_SETTINGS = {
    "auto_approve_pct": 0.02,       # <= 2% over the PO ...
    "auto_approve_abs": 100.00,     # ... and no more than $100 over -> auto-approve (with a note)
    "review_pct": 0.10,             # up to 10% over -> a person decides; beyond that -> reject / dispute
    "duplicate_window_days": 14,    # near-identical number + same amount within this many days = duplicate
    "ocr_min_confidence": 0.80,     # scans below this OCR confidence always go to a person
    "manual_minutes": 8,            # ROI ASSUMPTION: minutes of AP handling per invoice done by hand
}

MIN_FIELD_CONFIDENCE = 0.60   # below this an extracted field counts as unreliable
STAGE_DELAY_SEC = float(os.environ.get("APFLOW_STAGE_DELAY", "0.55"))  # makes the live view watchable
CROSSCHECK = os.environ.get("APFLOW_CROSSCHECK", "1") != "0"           # LLM cross-check when an API key is set
ANTHROPIC_MODEL = os.environ.get("APFLOW_MODEL", "claude-sonnet-5-5")
ADMIN_KEY = os.environ.get("APFLOW_ADMIN_KEY")                      # unset = editing thresholds is disabled

FLAG_LABELS = {
    "MISSING_INVOICE_NUMBER": "Missing invoice number", "MISSING_INVOICE_DATE": "Missing invoice date",
    "MISSING_TOTAL": "Missing total", "MISSING_VENDOR_NAME": "Missing vendor name",
    "LOW_CONFIDENCE": "Low extraction confidence", "LOW_OCR": "Low-quality scan",
    "ARITH_LINES": "Line items don't add up", "ARITH_TOTAL": "Total doesn't add up",
    "FUTURE_DATE": "Future-dated invoice", "STALE_DATE": "Very old invoice", "CURRENCY": "Non-USD currency",
    "VENDOR_UNKNOWN": "Vendor not identified", "VENDOR_NOT_APPROVED": "Vendor not approved",
    "VENDOR_NAME_FUZZY": "Vendor name variation",
    "PO_NOT_FOUND": "PO not found", "PO_VENDOR_MISMATCH": "PO belongs to another vendor", "PO_CLOSED": "PO closed",
    "PO_INFERRED": "No PO on the invoice", "PO_AMBIGUOUS": "Several POs could match", "PO_NONE": "No matching PO",
    "PO_EXHAUSTED": "PO already fully billed", "TAX_RATE": "Tax rate differs from PO",
    "OVER_TOLERANCE": "Over PO beyond tolerance", "FAR_OVER_PO": "Far over the PO",
    "DUPLICATE_EXACT": "Duplicate: same invoice number", "DUPLICATE_ALTERED": "Duplicate: altered invoice number",
    "DUPLICATE_SAME_AMOUNT": "Same amount, different number", "DUP_LIMITED": "Duplicate check limited",
    "UNREADABLE": "Document unreadable", "EXTRACT_DISAGREE": "Rules and LLM disagree",
    "CREDIT_NOTE": "Credit note, not payable",
}
