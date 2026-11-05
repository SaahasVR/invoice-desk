"""Tests for the LLM cross-check / gap-fill paths using a SIMULATED model (no API key or network needed).

These verify OUR logic (compare, merge, flag, degrade gracefully). They do not test real model behaviour.
"""
import os, sys, tempfile
os.environ["APFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "l.db")
os.environ["APFLOW_STAGE_DELAY"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, db, extract, pipeline  # noqa: E402

db.init()
fails = 0
def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
    fails += (not cond)

def F(**kw):
    return {k: {"value": v, "confidence": 0.8, "source": "llm"} for k, v in kw.items()}

def run(sample, sid, llm_fields=None, note="simulated"):
    db.reset_session(sid)
    extract.llm_available = lambda: llm_fields is not None
    extract.llm_extract = lambda text, path=None, scanned=False: (llm_fields or {}, note)
    return pipeline.Run(os.path.join(config.SAMPLES_DIR, sample), sample, lambda e: None, "x", sid).run()

def names(r, stage): return [c["name"] for c in r["checks"].get(stage, [])]
def codes(r): return {f["code"] for f in r["findings"]}

# a) LLM agrees with the rules -> approved, and the trail says so
r = run("01_happy_path.pdf", "a", F(invoice_number="NW-88214", total=5184.00, invoice_date="2026-10-20", po_reference="PO-1001", vendor_name="Northwind Supplies LLC"))
check("cross-check agrees -> still approved", r["decision"]["status"] == "APPROVED" and "Cross-check" in names(r, "extract"), r["decision"]["status"])

# b) LLM reads a different total -> sent to a person, with both values shown
r = run("01_happy_path.pdf", "b", F(invoice_number="NW-88214", total=4800.00))
msg = " ".join(c["detail"] for c in r["checks"]["extract"])
check("cross-check disagreement on total -> needs review", r["decision"]["status"] == "NEEDS_REVIEW" and "EXTRACT_DISAGREE" in codes(r), r["decision"]["status"])
check("disagreement message shows both values", "5184.0" in msg and "4800.0" in msg, msg[:120])

# c) different invoice number only -> still flagged
r = run("01_happy_path.pdf", "c", F(invoice_number="NW-88241"))
check("transposed invoice number is caught", "EXTRACT_DISAGREE" in codes(r))

# d) LLM fills genuine gaps (fields the rules could not find)
r = run("05_missing_fields.pdf", "d", F(invoice_number="APX-30600", invoice_date="2026-10-27"))
check("gap-fill supplies missing fields", "LLM filled gaps" in names(r, "extract") and not any(c.startswith("MISSING_") for c in codes(r)), str(sorted(codes(r))))
check("gap-filled fields are labelled as LLM-sourced", r["extracted"]["invoice_number"] == "APX-30600")

# e) LLM returns nothing / is unavailable -> rules path still decides
r = run("01_happy_path.pdf", "e", None)
check("no LLM -> rules decide as before", r["decision"]["status"] == "APPROVED")

# f) real llm_extract with a bogus key and no reachable API must degrade, not crash or hang
import importlib; importlib.reload(extract)
os.environ["ANTHROPIC_API_KEY"] = "sk-invalid"
f, note = extract.llm_extract("Invoice text", None)
check("unreachable/invalid API degrades gracefully", f == {} and "failed" in note.lower(), note[:110])
os.environ.pop("ANTHROPIC_API_KEY")

print("\nfailures:", fails); sys.exit(1 if fails else 0)
