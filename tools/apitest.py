"""End-to-end API tests against a running server (default http://127.0.0.1:8000).

Start the server with APFLOW_STAGE_DELAY=0 for speed, then:  python3 tools/apitest.py
Each Client() is a separate visitor (own cookie jar), which is how session isolation is tested.
"""
import http.cookiejar
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
ADMIN = os.environ.get("ADMIN_KEY", "admin")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _go(self, req):
        try:
            with self.op.open(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except Exception:
                return e.code, {}

    def get(self, path):
        return self._go(urllib.request.Request(BASE + path))

    def post_json(self, path, obj=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        return self._go(urllib.request.Request(BASE + path, data=json.dumps(obj or {}).encode(), headers=h, method="POST"))

    def post(self, path, fields=None, files=None):
        b = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (fields or {}).items():
            body.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fn, data) in (files or {}).items():
            body.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                       f'Content-Type: application/octet-stream\r\n\r\n'.encode())
            body.write(data)
            body.write(b"\r\n")
        body.write(f"--{b}--\r\n".encode())
        return self._go(urllib.request.Request(BASE + path, data=body.getvalue(), method="POST",
                                               headers={"Content-Type": f"multipart/form-data; boundary={b}"}))

    def wait(self, run_id):
        for _ in range(400):
            st, j = self.get(f"/api/runs/{run_id}")
            if st == 200:
                return j
            time.sleep(0.1)
        raise RuntimeError("timeout waiting for run " + run_id)

    def sample(self, sid):
        st, j = self.post("/api/run", {"sample_id": sid})
        assert st == 200, (st, j)
        return self.wait(j["run_id"])

    def upload(self, fn, data):
        st, j = self.post("/api/run", files={"file": (fn, data)})
        return (st, j) if st != 200 else (st, self.wait(j["run_id"]))


man = json.load(open(os.path.join(ROOT, "samples", "manifest.json")))
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"   [{extra}]" if extra else ""))
    fails += (not cond)


# 1. every sample, forward and reverse: results must not depend on run order
for label, order in (("forward", man), ("reverse", list(reversed(man)))):
    c = Client()
    c.post_json("/api/reset")
    for s in order:
        r = c.sample(s["id"])
        check(f"{label:7} {s['id']}", r["decision"] == s["expected"], f"got {r['decision']}")

# 2. resubmitting an approved invoice, and a still-pending one, is held as a duplicate
c = Client()
a, b = c.sample("01_happy_path"), c.sample("01_happy_path")
check("resubmitting an approved invoice is held", a["decision"] == "APPROVED" and b["decision"] == "ON_HOLD", f"{a['decision']} then {b['decision']}")
c = Client()
a, b = c.sample("02b_over_tolerance"), c.sample("02b_over_tolerance")
check("resubmitting an invoice still waiting for review is held", a["decision"] == "NEEDS_REVIEW" and b["decision"] == "ON_HOLD", f"{a['decision']} then {b['decision']}")

# 3. visitors are isolated: A's approved invoice must not make B's identical invoice a duplicate
A, B = Client(), Client()
ra = A.sample("01_happy_path")
rb = B.sample("01_happy_path")
check("visitor B is not affected by visitor A's ledger", ra["decision"] == "APPROVED" and rb["decision"] == "APPROVED", f"{ra['decision']} / {rb['decision']}")
check("visitor B cannot read visitor A's run", B.get(f"/api/runs/{ra['id']}")[0] == 404)
check("each visitor's dashboard only lists their own runs", len(A.get("/api/runs")[1]["runs"]) == 1 and len(B.get("/api/runs")[1]["runs"]) == 1)
check("reset clears only the caller's data", A.post_json("/api/reset")[0] == 200 and len(A.get("/api/runs")[1]["runs"]) == 0 and len(B.get("/api/runs")[1]["runs"]) == 1)

# 4. review actions
c = Client()
r = c.sample("02b_over_tolerance")
st, j = c.post_json(f"/api/runs/{r['id']}/review", {"action": "approve", "note": "Vendor confirmed the expedite fee"})
check("reviewer can approve an item that needs review", st == 200 and j["resolution"] == "APPROVED", str(j))
d = c.get(f"/api/runs/{r['id']}")[1]
check("resolution and note are stored", d["resolution"] == "APPROVED" and d["resolution_note"].startswith("Vendor confirmed"))
check("resolving twice is refused (409)", c.post_json(f"/api/runs/{r['id']}/review", {"action": "reject"})[0] == 409)
check("approved item counts toward the ledger (a re-send is now held)", c.sample("02b_over_tolerance")["decision"] == "ON_HOLD")
stats = c.get("/api/runs")[1]["stats"]
check("dashboard stats: 1 resolved, flags present", stats["resolved"] == 1 and len(stats["flags"]) >= 1, str({k: stats[k] for k in ("resolved", "pending")}))

c = Client()
ap = c.sample("01_happy_path")
check("an approved invoice cannot be reviewed (400)", c.post_json(f"/api/runs/{ap['id']}/review", {"action": "approve"})[0] == 400)
r7 = c.sample("07_no_po_reference")
check("unknown review action is refused (400)", c.post_json(f"/api/runs/{r7['id']}/review", {"action": "nuke"})[0] == 400)
check("another visitor cannot review my run (404)", Client().post_json(f"/api/runs/{r7['id']}/review", {"action": "approve"})[0] == 404)
c.post_json(f"/api/runs/{r7['id']}/review", {"action": "reject", "note": "wrong entity"})
check("a rejected (voided) invoice can be corrected and re-sent without a false duplicate hold", c.sample("07_no_po_reference")["decision"] == "NEEDS_REVIEW")
r5 = c.sample("05_missing_fields")
c.post_json(f"/api/runs/{r5['id']}/review", {"action": "request_info", "note": "asked vendor for number"})
check("'ask vendor' resolution is recorded", c.get(f"/api/runs/{r5['id']}")[1]["resolution"] == "INFO_REQUESTED")
rh = c.sample("03_duplicate_altered_number")
check("a held duplicate can be voided", c.post_json(f"/api/runs/{rh['id']}/review", {"action": "reject"})[0] == 200)

# 5. credit note links to the invoice it credits when that invoice is on file
c = Client()
c.sample("01_happy_path")
cn = c.sample("10_credit_note")
why = " ".join(cn["result"]["decision"]["reasons"])
check("credit note is never approved and links to the credited invoice", cn["decision"] == "NEEDS_REVIEW" and "on file" in why, why[:100])

# 6. settings: public read, admin-only write, validation, and a visible effect
st, j = c.get("/api/settings")
check("settings are readable by anyone", st == 200 and j["values"]["review_pct"] == 0.10)
body = {"auto_approve_pct": 0.0, "auto_approve_abs": 0.0, "review_pct": 0.10, "duplicate_window_days": 14, "ocr_min_confidence": 0.8, "manual_minutes": 8}
check("changing settings without the key is refused (403)", c.post_json("/api/settings", body)[0] == 403)
check("changing settings with a wrong key is refused (403)", c.post_json("/api/settings", body, {"X-Admin-Key": "nope"})[0] == 403)
check("invalid thresholds are refused (400)", c.post_json("/api/settings", {**body, "auto_approve_pct": 0.5, "review_pct": 0.1}, {"X-Admin-Key": ADMIN})[0] == 400)
try:
    check("admin can change thresholds", c.post_json("/api/settings", body, {"X-Admin-Key": ADMIN})[0] == 200)
    c2 = Client()
    check("tighter tolerance changes the outcome (0.5% over now needs review)", c2.sample("02a_within_tolerance")["decision"] == "NEEDS_REVIEW")
finally:
    c.post_json("/api/settings/reset", {}, {"X-Admin-Key": ADMIN})
check("restoring defaults restores the outcome", Client().sample("02a_within_tolerance")["decision"] == "APPROVED")

# 7. evaluation runs in isolation and does not touch the visitor's history
c = Client()
before = len(c.get("/api/runs")[1]["runs"])
st, ev = c.post_json("/api/eval")
check("evaluation: every built-in invoice matches", st == 200 and ev["matched"] == ev["total"] == len(man), f"{ev.get('matched')}/{ev.get('total')}")
check("evaluation does not add to the visitor's history", len(c.get("/api/runs")[1]["runs"]) == before)

# 8. bad uploads and path safety
c = Client()
st, j = c.upload("evil.pdf", b"not a pdf at all")
check("non-PDF bytes rejected with 400", st == 400, str(j))
st, j = c.upload("notes.txt", b"%PDF-1.4 fake")
check("wrong extension rejected with 400", st == 400, str(j))
check("file endpoint rejects path tricks", c.get("/api/runs/..%2f..%2fetc%2fpasswd/file")[0] in (404, 422))

# 9. invoices built on the fly
from tools import make_samples as ms  # noqa: E402


def custom(vendor_name, inv, date, po, subtotal, tax_rate):
    ms.VENDORS["VC"] = (vendor_name, "1 Test Road", "A")
    p = os.path.join(tempfile.mkdtemp(), "c.pdf")
    ms.draw(p, "VC", inv, date, po, [("Services", 1, subtotal)], tax_rate)
    with open(p, "rb") as f:
        return f.read()


c = Client()
r = c.upload("c1.pdf", custom("Northwind Supplies", "NW-90001", "2026-10-28", "PO-1007", 960.00, 0.08))[1]
check("registered alias ('Northwind Supplies') is still approved", r["decision"] == "APPROVED", f"got {r['decision']}")
r = c.upload("c2.pdf", custom("Northwnd Suplies LLC", "NW-90002", "2026-10-28", "PO-1001", 4800.00, 0.08))[1]
check("misspelled vendor is flagged, never auto-approved", r["decision"] in ("NEEDS_REVIEW", "REJECTED"), f"got {r['decision']}")
r = c.upload("c3.pdf", custom("Northwind Supplies LLC", "NW-90003", "2026-10-28", "PO-1004", 4000.00, 0.0))[1]
check("a PO that belongs to another vendor is flagged", r["decision"] in ("NEEDS_REVIEW", "REJECTED"), f"got {r['decision']}")
r = c.upload("c4.pdf", custom("Northwind Supplies LLC", "NW-90004", "2026-10-28", "PO-9999", 100.00, 0.0))[1]
check("an unknown PO number goes to review", r["decision"] == "NEEDS_REVIEW", f"got {r['decision']}")
r = c.upload("c5.pdf", custom("Northwind Supplies LLC", "NW-90005", "2026-10-28", "PO-1001", 9000.00, 0.08))[1]
check("an invoice far over the PO is rejected", r["decision"] == "REJECTED", f"got {r['decision']}")
r = c.upload("c6.pdf", custom("Northwind Supplies LLC", "NW-90006", "2027-12-01", "PO-1001", 4800.00, 0.08))[1]
check("a future-dated invoice goes to review", r["decision"] == "NEEDS_REVIEW", f"got {r['decision']}")

from reportlab.pdfgen import canvas  # noqa: E402

buf = io.BytesIO()
cv = canvas.Canvas(buf)
cv.drawString(72, 700, "Lunch menu: soup of the day, sandwiches and a very long list of unrelated words here.")
cv.save()
r = c.upload("menu.pdf", buf.getvalue())[1]
check("a non-invoice PDF does not crash and is not approved", r["decision"] in ("NEEDS_REVIEW", "REJECTED"), f"got {r['decision']}")

print("\nfailures:", fails)
sys.exit(1 if fails else 0)
