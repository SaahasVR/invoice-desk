"""The AP decision pipeline. Eight stages, each emitting live events:

  1 ingest -> 2 extract -> 3 validate -> 4 vendor -> 5 po_match -> 6 amounts -> 7 duplicates -> 8 decide

Decision precedence (strictest wins): REJECTED > ON_HOLD > NEEDS_REVIEW > APPROVED.
Every finding records a code, a severity and a plain-language message, so the final decision is
always traceable to the checks that caused it.
"""
import difflib
import re
import time
from datetime import date, datetime

from . import config, db, extract

STATUS_WORDS = {"PAID": "paid", "APPROVED": "approved", "PENDING": "waiting for review", "HELD": "on hold"}
STATUS_ORDER = {"APPROVED": 0, "NEEDS_REVIEW": 1, "ON_HOLD": 2, "REJECTED": 3}
SEV_TO_STATUS = {"info": "APPROVED", "review": "NEEDS_REVIEW", "hold": "ON_HOLD", "reject": "REJECTED"}


def money(v):
    return "n/a" if v is None else f"${v:,.2f}"


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _norm_inv(s):
    """Normalise an invoice number for comparison (case, punctuation, the classic O/0 swap)."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()).replace("O", "0")


def _edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _days_between(d1, d2):
    try:
        return abs((datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d2, "%Y-%m-%d")).days)
    except Exception:
        return None


class Run:
    def __init__(self, path, filename, emit, run_id, sid="local"):
        self.path, self.filename, self.emit, self.run_id, self.sid = path, filename, emit, run_id, sid
        self.cfg = db.get_settings()
        self.findings = []      # {code, severity, message, stage}
        self.checks = {}        # stage -> list of {name,state,detail}
        self.ctx = {}

    # ---- helpers
    def begin(self, stage, label):
        self.emit({"type": "stage", "stage": stage, "label": label, "state": "running"})
        time.sleep(config.STAGE_DELAY_SEC)

    def check(self, stage, name, state, detail, finding=None):
        """state: pass | warn | fail | info. finding=(code, severity) records a decision-relevant finding."""
        self.checks.setdefault(stage, []).append({"name": name, "state": state, "detail": detail})
        if finding:
            self.findings.append({"code": finding[0], "severity": finding[1], "message": detail, "stage": stage})

    def end(self, stage, label, summary, data=None):
        states = [c["state"] for c in self.checks.get(stage, [])]
        state = "fail" if "fail" in states else "warn" if "warn" in states else "pass"
        self.emit({"type": "stage", "stage": stage, "label": label, "state": state, "summary": summary,
                   "checks": self.checks.get(stage, []), "data": data or {}})

    # ---- stages
    def run(self):
        t0 = time.time()
        self.stage_ingest()
        self.stage_extract()
        self.stage_validate()
        self.stage_vendor()
        if self.ctx["ex"].get("doc_type") == "credit_note":
            self.credit_note_path()
        else:
            self.stage_po()
            self.stage_amounts()
            self.stage_duplicates()
        result = self.stage_decide()
        result["duration_ms"] = int((time.time() - t0) * 1000)
        return result

    def stage_ingest(self):
        self.begin("ingest", "Read document")
        doc = extract.read_document(self.path)
        self.ctx["doc"] = doc
        if doc["method"] == "text":
            self.check("ingest", "Text layer", "pass", f"Machine-readable PDF, {doc['pages']} page(s). Read text directly.")
        elif doc["method"] == "ocr":
            c = doc["ocr_confidence"]
            floor = self.cfg["ocr_min_confidence"]
            if c < floor:
                self.check("ingest", "Low-quality scan", "fail",
                           f"No text layer, so the page was OCR'd, but engine confidence is only {c:.0%} (minimum {floor:.0%}). "
                           "A person must confirm every value before anything is paid.", ("LOW_OCR", "review"))
            else:
                self.check("ingest", "Scanned image detected", "pass" if c >= 0.85 else "warn",
                           f"No text layer found, so the page was OCR'd (engine confidence {c:.0%}). "
                           "Every extracted value is trusted less than from a clean PDF.")
        else:
            self.check("ingest", "Document unreadable", "fail", "No text layer and OCR is unavailable.",
                       ("UNREADABLE", "review"))
        self.end("ingest", "Read document",
                 "Scanned image: OCR used" if doc["method"] == "ocr" else "Clean PDF: text layer read",
                 {"method": doc["method"], "ocr_confidence": doc["ocr_confidence"], "chars": len(doc["text"])})

    def stage_extract(self):
        self.begin("extract", "Extract fields")
        doc = self.ctx["doc"]
        fields = extract.parse_fields(doc["text"])
        if doc["method"] == "ocr":
            fields = extract.scale_for_ocr(fields, doc["ocr_confidence"])
        used_llm = False
        gaps = extract.needs_fallback(fields)
        if gaps or (extract.llm_available() and config.CROSSCHECK):
            llm_fields, note = extract.llm_extract(doc["text"], self.path, scanned=doc["method"] == "ocr")
            if llm_fields:
                agreed, diffs = extract.compare(fields, llm_fields)   # compare BEFORE merging
                fields, filled = extract.merge(fields, llm_fields)
                used_llm = bool(filled)
                if filled:
                    self.check("extract", "LLM filled gaps", "info", f"{note} Filled: {', '.join(filled)}.")
                for fld, rv, lv in diffs:
                    self.check("extract", f"Cross-check: {fld.replace('_', ' ')}", "fail",
                               f"Rules read '{rv}' but the LLM read '{lv}'. They disagree, so a person should look at the original.",
                               ("EXTRACT_DISAGREE", "review"))
                if agreed and not diffs:
                    self.check("extract", "Cross-check", "pass", f"Rules and LLM agree on {len(agreed)} field(s): {', '.join(agreed)}.")
            else:
                self.check("extract", "LLM fallback", "info", f"Rules left gaps in required fields. {note}" if gaps else note)
        self.ctx["fields"] = fields
        g = lambda k: (fields.get(k) or {}).get("value")
        ex = {k: g(k) for k in ["vendor_name", "invoice_number", "invoice_date", "po_reference", "currency",
                                  "subtotal", "tax", "total"]}
        ex["line_items"] = g("line_items") or []
        ex["doc_type"] = g("doc_type") or "invoice"
        ex["original_invoice"] = g("original_invoice")
        self.ctx["ex"] = ex
        found = [k for k in ["vendor_name", "invoice_number", "invoice_date", "po_reference", "total"] if g(k) is not None]
        self.check("extract", "Fields found", "pass" if len(found) >= 4 else "warn",
                   f"{len(found)} of 5 key fields found by {'rules + LLM' if used_llm else 'rule-based parsing'}: "
                   f"{', '.join(found) or 'none'}.")
        self.end("extract", "Extract fields", f"{len(found)}/5 key fields extracted",
                 {"fields": {k: {"value": v["value"] if k != "line_items" else f"{len(v['value'])} lines",
                                 "confidence": v["confidence"], "source": v["source"]} for k, v in fields.items()}})

    def stage_validate(self):
        self.begin("validate", "Validate completeness")
        f, ex = self.ctx["fields"], self.ctx["ex"]
        questions = []
        labels = {"invoice_number": "invoice number", "invoice_date": "invoice date", "total": "total amount",
                  "vendor_name": "vendor name"}
        for k, lab in labels.items():
            if ex.get(k) in (None, ""):
                self.check("validate", f"Has {lab}", "fail", f"The invoice has no {lab}.", (f"MISSING_{k.upper()}", "review"))
                questions.append(f"Please confirm the {lab}.")
            else:
                self.check("validate", f"Has {lab}", "pass", f"{lab.capitalize()}: {ex[k] if k != 'total' else money(ex[k])}")
        # low-confidence fields
        low = [k for k in labels if k in f and f[k]["confidence"] < config.MIN_FIELD_CONFIDENCE]
        if low:
            self.check("validate", "Field confidence", "warn",
                       f"Low extraction confidence on: {', '.join(low)}. A person should eyeball these.", ("LOW_CONFIDENCE", "review"))
        # arithmetic
        items, sub, tax, tot = ex["line_items"], ex["subtotal"], ex["tax"], ex["total"]
        if items and sub is not None:
            ls = round(sum(i["amount"] for i in items), 2)
            ok = abs(ls - sub) <= 0.02
            self.check("validate", "Lines add up to subtotal", "pass" if ok else "fail",
                       f"Line items sum to {money(ls)}; subtotal says {money(sub)}." + ("" if ok else " Mismatch."),
                       None if ok else ("ARITH_LINES", "review"))
        if sub is not None and tot is not None:
            expect = round(sub + (tax or 0), 2)
            ok = abs(expect - tot) <= 0.02
            self.check("validate", "Subtotal + tax = total", "pass" if ok else "fail",
                       f"{money(sub)} + {money(tax or 0)} = {money(expect)}; invoice total is {money(tot)}." + ("" if ok else " Mismatch."),
                       None if ok else ("ARITH_TOTAL", "review"))
        # sanity: date and currency
        d = ex["invoice_date"]
        if d:
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
                age = (date.today() - dd).days
                if dd > date.today() and (dd - date.today()).days > 2:
                    self.check("validate", "Invoice date plausible", "fail", f"Invoice is dated in the future ({d}).", ("FUTURE_DATE", "review"))
                elif age > 365:
                    self.check("validate", "Invoice date plausible", "warn", f"Invoice is {age} days old ({d}).", ("STALE_DATE", "review"))
                else:
                    self.check("validate", "Invoice date plausible", "pass", f"Dated {d}.")
            except ValueError:
                pass
        if ex["currency"] and ex["currency"] != "USD":
            self.check("validate", "Currency", "warn", f"Invoice currency is {ex['currency']}; this process handles USD only.", ("CURRENCY", "review"))
        self.ctx["questions"] = questions
        n_issue = sum(1 for c in self.checks["validate"] if c["state"] in ("fail", "warn"))
        self.end("validate", "Validate completeness", "All required fields present and consistent" if not n_issue else f"{n_issue} issue(s) found")

    def stage_vendor(self):
        self.begin("vendor", "Check vendor")
        name = self.ctx["ex"]["vendor_name"]
        vendors = db.load_vendors()
        best, best_score, how = None, 0.0, None
        for v in vendors:
            for cand in [v["name"]] + v["aliases"]:
                if _norm(cand) == _norm(name):
                    best, best_score, how = v, 1.0, "exact name/alias match"
                    break
                s = difflib.SequenceMatcher(None, _norm(cand), _norm(name)).ratio()
                if s > best_score:
                    best, best_score, how = v, s, f"fuzzy match ({s:.0%})"
            if best_score == 1.0 and best is v:
                break
        self.ctx["vendor"] = None
        if not name:
            self.check("vendor", "Vendor identified", "fail", "No vendor name to check against the approved-vendor master.", ("VENDOR_UNKNOWN", "review"))
        elif best is None or best_score < 0.88:
            self.check("vendor", "On approved-vendor master", "fail",
                       f"'{name}' is not in the approved-vendor master. A valid PO number on the invoice does not make a vendor legitimate.",
                       ("VENDOR_NOT_APPROVED", "reject"))
        else:
            self.ctx["vendor"] = best
            if not best["approved"]:
                self.check("vendor", "Vendor approved", "fail", f"{best['name']} is on the master but is not approved for payment.", ("VENDOR_NOT_APPROVED", "reject"))
            else:
                self.check("vendor", "On approved-vendor master", "pass", f"Matched to {best['name']} ({best['vendor_id']}) via {how}.")
                if best_score < 1.0:
                    self.check("vendor", "Name variation", "warn", f"Invoice name '{name}' is not exactly the registered name; confirm it's the same entity.", ("VENDOR_NAME_FUZZY", "review"))
        v = self.ctx["vendor"]
        self.end("vendor", "Check vendor", f"Matched {v['name']}" if v else "Vendor not recognised",
                 {"vendor_id": v["vendor_id"] if v else None})

    def stage_po(self):
        self.begin("po_match", "Match purchase order")
        ex, v = self.ctx["ex"], self.ctx["vendor"]
        pos = db.load_pos()
        po, ref = None, ex["po_reference"]
        self.ctx["po"], self.ctx["po_inferred"] = None, False
        if ref:
            po = next((p for p in pos if p["po_number"].upper() == ref.upper()), None)
            if not po:
                self.check("po_match", "PO exists", "fail", f"Invoice quotes {ref}, which is not in the PO register.", ("PO_NOT_FOUND", "review"))
            else:
                self.check("po_match", "PO exists", "pass", f"{ref} found: {po['description']} ({money(po['net_amount'])} net).")
                if v and po["vendor_id"] != v["vendor_id"]:
                    owner = next((x["name"] for x in db.load_vendors() if x["vendor_id"] == po["vendor_id"]), po["vendor_id"])
                    self.check("po_match", "PO belongs to this vendor", "fail",
                               f"{ref} was issued to {owner}, not {v['name']}. Possible mis-keyed reference or fraud attempt.",
                               ("PO_VENDOR_MISMATCH", "reject" if not v["approved"] else "review"))
                elif v:
                    self.check("po_match", "PO belongs to this vendor", "pass", f"{ref} was issued to {v['name']}.")
                if po["status"] != "open":
                    self.check("po_match", "PO is open", "fail", f"{ref} is {po['status']}.", ("PO_CLOSED", "review"))
        elif v and ex["total"] is not None:
            cands = []
            for p in pos:
                if p["vendor_id"] != v["vendor_id"] or p["status"] != "open":
                    continue
                rem = self._remaining(p)
                if rem > 0 and abs(ex["total"] - rem) / p["gross_amount"] <= self.cfg['review_pct']:
                    cands.append((p, rem))
            if len(cands) == 1:
                po, rem = cands[0]
                self.ctx["po_inferred"] = True
                self.check("po_match", "PO reference missing", "warn",
                           f"No PO on the invoice. Inferred {po['po_number']} ({po['description']}) because it is the only open {v['name']} PO "
                           f"whose remaining value ({money(rem)}) fits the invoice total. An inferred match is a suggestion, not proof.",
                           ("PO_INFERRED", "review"))
            elif len(cands) > 1:
                self.check("po_match", "PO reference missing", "fail",
                           f"No PO on the invoice and {len(cands)} open POs could fit: {', '.join(p['po_number'] for p, _ in cands)}.", ("PO_AMBIGUOUS", "review"))
            else:
                self.check("po_match", "PO reference missing", "fail", "No PO on the invoice and no open PO for this vendor fits the amount.", ("PO_NONE", "review"))
        else:
            self.check("po_match", "PO reference missing", "fail", "No PO on the invoice and not enough information to infer one.", ("PO_NONE", "review"))
        self.ctx["po"] = po
        self.end("po_match", "Match purchase order",
                 (f"{po['po_number']} {'(inferred)' if self.ctx['po_inferred'] else 'matched'}" if po else "No PO matched"),
                 {"po_number": po["po_number"] if po else None, "inferred": self.ctx["po_inferred"]})

    def _remaining(self, po):
        billed = sum(r["total"] for r in db.billed_against_po(self.sid, po["po_number"]))
        return round(po["gross_amount"] - billed, 2)

    def stage_amounts(self):
        self.begin("amounts", "Check amounts")
        ex, po = self.ctx["ex"], self.ctx["po"]
        self.ctx["balance"] = None
        if not po or ex["total"] is None:
            self.check("amounts", "Amount vs PO", "info", "Skipped: needs both a matched PO and an invoice total.")
            self.end("amounts", "Check amounts", "Skipped (no PO or total)")
            return
        prior = db.billed_against_po(self.sid, po["po_number"])
        billed = round(sum(r["total"] for r in prior), 2)
        remaining = round(po["gross_amount"] - billed, 2)
        total = ex["total"]
        self.check("amounts", "PO ledger", "info",
                   f"{po['po_number']} is worth {money(po['gross_amount'])} incl. {po['tax_rate']*100:g}% tax. "
                   f"{money(billed)} already billed across {len(prior)} invoice(s); {money(remaining)} remains.")
        # tax rate sanity
        if ex["subtotal"] and ex["tax"] is not None and ex["subtotal"] > 0:
            rate = ex["tax"] / ex["subtotal"]
            if abs(rate - po["tax_rate"]) > 0.005:
                self.check("amounts", "Tax rate matches PO", "warn", f"Invoice tax is {rate*100:.1f}% but the PO assumed {po['tax_rate']*100:g}%.", ("TAX_RATE", "review"))
            else:
                self.check("amounts", "Tax rate matches PO", "pass", f"Tax is {rate*100:g}%, as the PO assumed.")
        over = round(total - remaining, 2)
        pct = over / po["gross_amount"]
        if remaining <= 0:
            self.check("amounts", "PO has balance", "fail", f"{po['po_number']} is already fully billed; this invoice has nothing left to draw on.", ("PO_EXHAUSTED", "review"))
        elif over <= 0:
            left = round(remaining - total, 2)
            kind = "Invoice matches the remaining PO value exactly." if abs(over) < 0.01 else f"Partial billing: {money(total)} of {money(remaining)} remaining."
            self.check("amounts", "Within PO balance", "pass", f"{kind} Balance after this invoice: {money(left)}.")
            self.ctx["balance"] = left
        elif over <= self.cfg['auto_approve_abs'] and pct <= self.cfg['auto_approve_pct']:
            self.check("amounts", "Within tolerance", "warn",
                       f"{money(over)} over the remaining PO value ({pct:.2%} of PO). Inside the auto-approve band "
                       f"(up to {self.cfg['auto_approve_pct']:.0%} and {money(self.cfg['auto_approve_abs'])}).")
            self.ctx["balance"] = 0.0
            self.ctx["within_tolerance"] = over
        elif pct <= self.cfg['review_pct']:
            self.check("amounts", "Over tolerance", "fail",
                       f"{money(over)} over the remaining PO value ({pct:.1%} of PO). Beyond the auto-approve band, so a person decides whether to accept, partially pay or dispute.",
                       ("OVER_TOLERANCE", "review"))
        else:
            self.check("amounts", "Far over PO", "fail",
                       f"{money(over)} over the remaining PO value ({pct:.1%} of PO), beyond the {self.cfg['review_pct']:.0%} dispute threshold.", ("FAR_OVER_PO", "reject"))
        self.ctx["amounts"] = {"billed": billed, "remaining": remaining, "over": over, "pct": round(pct, 4)}
        self.end("amounts", "Check amounts", f"Invoice {money(total)} vs {money(remaining)} remaining on {po['po_number']}", self.ctx["amounts"])

    def stage_duplicates(self):
        self.begin("duplicates", "Check duplicates")
        ex, v = self.ctx["ex"], self.ctx["vendor"]
        hits = []
        if not v:
            self.check("duplicates", "History compare", "info", "Skipped: vendor not identified, so no vendor history to compare against.")
        else:
            inv, tot, dt = ex["invoice_number"], ex["total"], ex["invoice_date"]
            ni = _norm_inv(inv)
            for r in db.ledger_for_vendor(self.sid, v["vendor_id"]):
                nr = _norm_inv(r["invoice_number"])
                same_amt = tot is not None and r["total"] is not None and abs(r["total"] - tot) < 0.01
                gap = _days_between(dt, r["invoice_date"]) if dt else None
                close_date = gap is not None and gap <= self.cfg['duplicate_window_days']
                if ni and ni == nr:
                    hits.append((r, "Same invoice number already on file.", "exact"))
                elif ni and same_amt and close_date and (nr in ni or ni in nr or _edit_distance(ni, nr) <= 2):
                    when = "the same date" if gap == 0 else f"a date {gap} day(s) apart"
                    hits.append((r, f"Invoice number '{inv}' is nearly identical to '{r['invoice_number']}', with the same amount "
                                    f"({money(tot)}) and {when}.", "altered"))
                elif same_amt and gap is not None and gap <= 3 and ni != nr:
                    when = "on the same date" if gap == 0 else f"within {gap} day(s)"
                    hits.append((r, f"Same amount ({money(tot)}) as '{r['invoice_number']}' {when}, under a different number.", "same_amount"))
            if not inv:
                self.check("duplicates", "Number-based check", "warn", "No invoice number, so the strongest duplicate check can't run.", ("DUP_LIMITED", "review"))
            if hits:
                for r, msg, kind in hits:
                    sev = "hold"
                    self.check("duplicates", "Possible duplicate", "fail",
                               f"{msg} Earlier invoice: {r['invoice_number']} ({STATUS_WORDS.get(r['status'], r['status'].lower())}, {money(r['total'])}).", ("DUPLICATE_" + kind.upper(), sev))
            else:
                n_prior = len(db.ledger_for_vendor(self.sid, v["vendor_id"]))
                if n_prior == 0:
                    self.check("duplicates", "No prior invoices", "info",
                               f"No earlier invoices from {v['name']} are on file, so there is nothing to compare against.")
                else:
                    self.check("duplicates", "No duplicate found", "pass",
                               f"Compared against {n_prior} prior invoice(s) from this vendor on number, amount and date.")
        self.ctx["dups"] = [{"invoice_number": r["invoice_number"], "reason": m, "kind": k} for r, m, k in hits]
        if hits:
            summ = "Possible duplicate found"
        elif not ex["invoice_number"]:
            summ = "Duplicate check limited: no invoice number"
        else:
            summ = "No duplicates"
        self.end("duplicates", "Check duplicates", summ, {"matches": self.ctx["dups"]})

    def skip_stage(self, stage, label, why):
        self.begin(stage, label)
        self.check(stage, label, "info", why)
        self.end(stage, label, "Skipped for credit notes")

    def credit_note_path(self):
        """A credit note reduces what we owe. It is never paid, and its amount must not be compared with a PO."""
        ex, v = self.ctx["ex"], self.ctx["vendor"]
        self.begin("po_match", "Match purchase order")
        orig = ex.get("original_invoice")
        found = None
        if v and orig:
            found = next((r for r in db.ledger_for_vendor(self.sid, v["vendor_id"])
                          if _norm_inv(r["invoice_number"]) == _norm_inv(orig)), None)
        if found:
            self.check("po_match", "Original invoice", "pass",
                       f"Refers to {orig}, which is on file ({STATUS_WORDS.get(found['status'], found['status'].lower())}, {money(found['total'])}). "
                       f"This credit of {money(ex['total'])} reduces it.")
        elif orig:
            self.check("po_match", "Original invoice", "warn", f"Refers to {orig}, which is not on file for this vendor.")
        else:
            self.check("po_match", "Original invoice", "warn", "No original invoice number quoted, so it can't be linked to a payment.")
        self.check("po_match", "Document type", "fail",
                   f"This is a credit note for {money(ex['total'])}, not a payable invoice. Paying it would pay the vendor twice.",
                   ("CREDIT_NOTE", "review"))
        self.ctx["po"], self.ctx["po_inferred"] = None, False
        self.end("po_match", "Match purchase order", "Credit note: not payable")
        self.skip_stage("amounts", "Check amounts", "A credit note is not compared with a PO balance.")
        self.skip_stage("duplicates", "Check duplicates", "Amount-based duplicate checks would wrongly match the invoice it credits.")

    # ---- final decision
    def stage_decide(self):
        self.begin("decide", "Decide")
        sev_rank = {"info": 0, "review": 1, "hold": 2, "reject": 3}
        blockers = [f for f in self.findings if f["severity"] != "info"]
        top = max((sev_rank[f["severity"]] for f in blockers), default=0)
        status = {0: "APPROVED", 1: "NEEDS_REVIEW", 2: "ON_HOLD", 3: "REJECTED"}[top]
        ex, po, v = self.ctx["ex"], self.ctx["po"], self.ctx["vendor"]
        reasons = [f["message"] for f in sorted(blockers, key=lambda x: -sev_rank[x["severity"]])]
        actions, message = [], None
        inv = ex["invoice_number"] or "(no number)"
        vend = (v["name"] if v else ex["vendor_name"]) or "the vendor"

        if status == "APPROVED":
            headline = f"Approved for payment: {money(ex['total'])} to {vend} against {po['po_number']}"
            bal = self.ctx.get("balance")
            reasons = [f"Vendor is on the approved master.", f"{po['po_number']} is open and the amount fits its remaining balance."
                       if not self.ctx.get("within_tolerance") else f"Invoice is {money(self.ctx['within_tolerance'])} over the PO, inside the auto-approve tolerance.",
                       "No duplicate found in this vendor's history.", "All required fields present and arithmetic checks out."]
            if self.ctx["doc"]["method"] == "ocr":
                reasons.append("Document was a scan; OCR confidence was high enough and the totals reconciled.")
            if bal is not None:
                reasons.append(f"PO balance after this invoice: {money(bal)}.")
            actions = ["Schedule payment per vendor terms.", "Ledger updated so later invoices see this one."]
            db.add_ledger(self.sid, v["vendor_id"], ex["invoice_number"], ex["invoice_date"], ex["total"], po["po_number"], self.run_id)
        elif status == "NEEDS_REVIEW":
            codes = {f["code"] for f in blockers}
            headline = f"Needs human review: {self._short(blockers)}"
            if "CREDIT_NOTE" in codes:
                orig = ex.get("original_invoice") or "the original invoice"
                reasons = [f"This is a credit note for {money(ex['total'])} from {vend}, not a payable invoice."]
                reasons += [c["detail"] for c in self.checks.get("po_match", []) if c["name"] == "Original invoice"]
                actions = ["Do not pay.", f"Apply the credit against {orig} in the ERP, or net it off the vendor's next payment."]
            elif "LOW_OCR" in codes:
                actions = ["Open the original scan next to the extracted values and confirm each one.",
                           "If anything is unreadable, ask the vendor for a clean copy."]
            elif any(c.startswith("MISSING_") for c in codes):
                qs = self.ctx["questions"]
                message = {"to": vend, "subject": f"Invoice from {vend}: information needed",
                           "body": f"Hello,\n\nWe received your invoice but cannot process it yet. Could you please resend it with the following:\n"
                                   + "\n".join(f"  - {q}" for q in qs)
                                   + (f"\n\nFor reference we matched it to {po['po_number']}." if po else "")
                                   + "\n\nWe'll process it as soon as we have the corrected invoice.\n\nAccounts Payable"}
                actions = ["Draft email asking the vendor for the missing details is ready below. Nothing has been sent yet.",
                           "Invoice parked; no payment scheduled."]
            elif "PO_INFERRED" in codes:
                actions = [f"Confirm with the requester that {po['po_number']} is the right PO.", "If confirmed, approve; otherwise ask the vendor for the PO reference."]
                message = {"to": vend, "subject": f"Invoice {inv}: PO reference needed",
                           "body": f"Hello,\n\nInvoice {inv} for {money(ex['total'])} does not quote a purchase order number. Please confirm which PO it relates to "
                                   f"(we believe it may be {po['po_number']}) and quote it on future invoices.\n\nAccounts Payable"}
            else:
                actions = ["Route to the PO owner / AP lead with the reasons below.", "Options: accept, pay partially, or dispute with the vendor."]
        elif status == "ON_HOLD":
            headline = f"Held as a possible duplicate: {inv} from {vend}"
            actions = ["Do not pay. Compare against the earlier invoice shown below.", "If it is a re-send, void it; if genuinely new, release manually."]
            message = {"to": "AP team (internal)", "subject": f"Duplicate hold: {vend} {inv}",
                       "body": f"Invoice {inv} ({money(ex['total'])}) was held automatically.\n\n" + "\n".join(f"- {r}" for r in reasons)
                               + "\n\nPlease confirm with the vendor whether this is a re-send."}
        else:
            headline = f"Rejected: {self._short(blockers)}"
            actions = ["Do not pay.", "Notify the vendor and, if the vendor is unknown, flag to procurement/security."]
            message = {"to": vend, "subject": f"Invoice {inv} could not be processed",
                       "body": f"Hello,\n\nWe are unable to process invoice {inv}.\n\n" + "\n".join(f"- {r}" for r in reasons)
                               + "\n\nIf you believe this is an error, please contact Accounts Payable.\n\nAccounts Payable"}

        conf = "high" if status == "APPROVED" and not self.ctx.get("within_tolerance") and self.ctx["doc"]["method"] == "text" else \
               "medium" if status == "APPROVED" else "high" if status in ("REJECTED", "ON_HOLD") else "medium"
        is_credit = ex.get("doc_type") == "credit_note"
        if status in ("NEEDS_REVIEW", "ON_HOLD") and v and not is_credit and ex["total"] is not None:
            # record it so a second copy of an invoice that is still waiting is caught as a duplicate too
            db.add_ledger(self.sid, v["vendor_id"], ex["invoice_number"], ex["invoice_date"], ex["total"],
                          po["po_number"] if po else None, self.run_id, "PENDING" if status == "NEEDS_REVIEW" else "HELD")
        decision = {"status": status, "headline": headline, "reasons": reasons, "actions": actions, "message": message,
                    "confidence": conf, "po_number": po["po_number"] if po else None,
                    "vendor_id": v["vendor_id"] if v else None, "kind": "credit_note" if is_credit else "invoice"}
        result = {"decision": decision, "extracted": ex, "checks": self.checks, "findings": self.findings,
                  "amounts": self.ctx.get("amounts"), "filename": self.filename}
        self.emit({"type": "stage", "stage": "decide", "label": "Decide",
                   "state": {"APPROVED": "pass", "NEEDS_REVIEW": "warn", "ON_HOLD": "fail", "REJECTED": "fail"}[status],
                   "summary": headline, "checks": [], "data": {}})
        self.emit({"type": "decision", "result": result})
        return result

    @staticmethod
    def _short(blockers):
        top = sorted(blockers, key=lambda f: -{"info": 0, "review": 1, "hold": 2, "reject": 3}[f["severity"]])[0]
        return {"VENDOR_NOT_APPROVED": "vendor is not on the approved master",
                "OVER_TOLERANCE": "amount is over the PO beyond tolerance",
                "FAR_OVER_PO": "amount is far over the PO",
                "PO_EXHAUSTED": "PO is already fully billed",
                "PO_INFERRED": "no PO on the invoice (one was inferred)",
                "PO_NONE": "no matching PO",
                "PO_NOT_FOUND": "PO reference not found",
                "PO_AMBIGUOUS": "several POs could match",
                "PO_VENDOR_MISMATCH": "PO belongs to a different vendor",
                "ARITH_TOTAL": "invoice arithmetic does not add up",
                "ARITH_LINES": "line items do not add up",
                "LOW_CONFIDENCE": "low extraction confidence",
                "LOW_OCR": "the scan is too poor to trust automatically",
                "EXTRACT_DISAGREE": "the rules and the LLM read different values",
                "CREDIT_NOTE": "this is a credit note, not a payable invoice",
                "UNREADABLE": "the document could not be read"}.get(top["code"],
               "required invoice details are missing" if top["code"].startswith("MISSING_") else top["message"][:80])
