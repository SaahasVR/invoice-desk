# Invoice desk: PDF invoice to a reasoned AP decision

A working process for the Zamp AI Solutions Analyst case study, **PS-1: invoice processing, from PDF to decision**.

Upload (or pick) a vendor invoice PDF. The process reads it, checks it against the vendor master, the PO register and the payment ledger, and returns one of four decisions with the reasoning visible at every step. A live audit trail shows each stage as it runs. A person can resolve anything that needs one, a dashboard keeps the history, and every rule is written out in plain language.

| Decision | Meaning |
|---|---|
| **Approved** | Safe to pay. The ledger is updated so later invoices see it. |
| **Needs review** | A person must decide. Reasons, next steps and a draft message are provided. |
| **On hold** | Possible duplicate. Do not pay until compared with the earlier invoice. |
| **Rejected** | Do not pay (unknown vendor, or far over the PO). |

## The four screens

- **Process an invoice**: inbox of samples (or drop your own PDF), live audit trail, decision. For anything waiting on a person there are **Approve / Reject / Ask the vendor** buttons; the choice and a note are logged.
- **Dashboard**: every run with its decision and reason, who resolved what, why invoices get flagged most, and an estimated time saving. Click a row to replay its trail.
- **Rules**: the decision logic in plain language, with the live thresholds filled in. Open the page with `?admin=YOUR_KEY` to edit them (see *Deploy*).
- **Evaluation**: runs all built-in invoices in a clean sandbox and shows expected vs actual.

Each visitor gets an isolated sandbox (cookie-based): their runs and ledger are their own, so two people trying the same link never make each other's invoices look like duplicates. "Reset my data" only clears your own.

## Run it

```bash
pip install -r requirements.txt       # needs tesseract-ocr and poppler-utils on the host for scanned invoices
python3 tools/make_samples.py         # only if you want to regenerate the sample invoices
uvicorn app.main:app --port 8000      # then open http://localhost:8000
```

Optional environment variables:

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Turns on the LLM: fills gaps the rules miss, and cross-checks values the rules found. |
| `APFLOW_ADMIN_KEY` | Key for editing thresholds. **Unset means editing is disabled** (the safe default for a public link). |
| `APFLOW_STAGE_DELAY` | Seconds between stages (default 0.55 so the live view is watchable; 0 for tests). |
| `APFLOW_CROSSCHECK=0` | Use the LLM for gaps only, not as a second opinion. |

**Tests** (start the server with `APFLOW_STAGE_DELAY=0` first):

```bash
python3 tools/selftest.py     # 13 samples through the engine, offline
APFLOW_ADMIN_KEY=admin ...    # start the server with this set so the settings tests can run
python3 tools/apitest.py      # end-to-end API: sessions, review, settings, evaluation, bad uploads, adversarial invoices
python3 tools/llmtest.py      # cross-check and gap-fill logic against a SIMULATED model
```

## Process map

```
PDF -> 1 Read -> 2 Extract -> 3 Validate -> 4 Vendor -> 5 PO match -> 6 Amounts -> 7 Duplicates -> 8 Decide
```

1. **Read.** Text PDFs are read directly (all pages). Image-only PDFs are OCR'd with Tesseract; every value is trusted less, and a scan under the minimum OCR confidence always goes to a person.
2. **Extract.** A rule-based parser handles several layouts (different labels, tax separate or embedded). Each field carries a confidence and a source. With an API key set, Claude also reads the invoice: it fills gaps the rules missed and **cross-checks** values the rules found; any disagreement goes to a person.
3. **Validate.** Required fields present; line items add up to the subtotal; subtotal + tax = total; date plausible; USD only.
4. **Vendor.** Matched to the approved-vendor master by name or alias (fuzzy at 88%+). Not on the master means reject; a near-match means review.
5. **PO match.** The PO must exist, belong to this vendor and be open. If missing, a PO is *suggested* from vendor + amount but never auto-approved. Credit notes skip this and the next two stages (they are never paid).
6. **Amounts.** Invoice total vs the PO's *remaining* gross value (PO net + tax, minus everything already billed).
7. **Duplicates.** Against this vendor's ledger (paid, approved, **waiting for review and held** invoices) on invoice number, amount and date.
8. **Decide.** Strictest finding wins: Rejected > On hold > Needs review > Approved. Output includes reasons, next actions and a draft message.

If the process itself errors, the invoice goes to **Needs review**. Nothing is silently approved or dropped.

## Edge cases (all built, all in the sample inbox)

| Sample | What it tests | Expected |
|---|---|---|
| `01_happy_path` | Clean invoice, exact PO match | Approved |
| `02a_within_tolerance` | $68.75 over the PO including tax (0.5%), inside the auto-approve band | Approved, with a note |
| `02b_over_tolerance` | 8% over the PO | Needs review |
| `03_duplicate_altered_number` | `INV-2041A` re-sends already-paid `INV-2041` (same vendor, amount, date) | On hold |
| `04a_split_po_ok` | Second milestone invoice on a split PO; remaining balance tracked | Approved |
| `04b_split_po_overrun` | Milestone invoice that pushes cumulative billing past the PO | Needs review |
| `05_missing_fields` | No invoice number and no date | Needs review + vendor email asking for exactly what's missing |
| `06_scanned_image` | Clean image-only scan (OCR needed) | Approved after OCR and reconciliation |
| `07_no_po_reference` | No PO on the invoice; PO inferred from vendor + amount | Needs review (suggested PO) |
| `08_unapproved_vendor` | Unknown vendor quoting a real PO number | Rejected |
| `09_multipage` | 30 line items over two pages, totals only on page 2 | Approved |
| `10_credit_note` | A credit note must never be paid; linked to the invoice it credits | Needs review |
| `11_rough_scan` | Tilted, shadowed, low-resolution "phone photo" (OCR confidence about 48%) | Needs review: scan too poor to trust |

Sample 11 is a *simulated* rough scan I generated. Real phone photos vary; the point is that low-quality input is routed to a person instead of being trusted.

For the **demo**, the strongest are `03`, `04a`/`04b`, `05`, `10` and `11`: they show deliberate logic rather than simple validation.

## Assumptions

- **PO amounts are net of tax.** The PO register stores net and tax rate; the process compares the invoice total with the *gross* remaining balance, so tax-embedded and tax-separate invoices both work.
- **Tolerance:** auto-approve if over by no more than 2% of the PO value *and* $100; up to 10% goes to a person; more is rejected as a dispute. Measured against the PO value (the commitment), not the remaining balance. All illustrative and editable on the Rules tab.
- **Duplicates:** same vendor and (same normalised invoice number, or a near-identical number with the same amount within 14 days, or the same amount within 3 days under a different number). Sequential same-amount milestones weeks apart are not flagged. Invoices waiting for review and held ones count; voided or "vendor asked for info" ones don't, so a corrected re-send isn't blocked by its predecessor.
- **Counted against a PO:** invoices that are paid or approved (including approved by a person).
- **Credit notes** are never auto-processed. A person applies them.
- **A scan under 80% OCR confidence** always goes to a person.
- **LLM-filled fields** are trusted at 80% confidence, and the arithmetic checks still have to reconcile. A stricter policy (always review LLM-only values) is a one-line change.
- **Time saved** uses an assumed 8 minutes of manual handling per invoice. It is an assumption, not a measurement.
- **USD only**; US date convention for `MM/DD/YYYY`.
- **Draft messages are never sent.**
- The vendor master, PO register and payment history are small files in `data/` standing in for the client's ERP.

## Honest limitations

- **The LLM path has never run against a real model.** The client was exercised up to authentication (a fake key returns a genuine 401, handled gracefully), and the cross-check and gap-fill logic is tested against a *simulated* model. Real model behaviour is untested.
- **The Evaluation tab is a regression check, not an accuracy score.** The samples are the cases the rules were built around, and the mix is edge-case heavy, so its "approved without a person" figure is not a real-world rate. Measuring true straight-through rate needs real invoices.
- **All sample invoices come from one generator** (three vendor layouts). Unseen layouts are exactly when the LLM and the human-review route matter.
- Vendor identity is name-based. Bank-account and tax-ID verification (the PS-2 problem) is not done.
- No ERP read-back or write-back, no real email, no login, one currency.
- SQLite on a free host is ephemeral: history clears on redeploy.

## Deploy for a public link

The app needs Tesseract and Poppler, so deploy it as a container (the `Dockerfile` handles that).

**Render (free):** push this folder to a GitHub repo, then New > Blueprint (or New > Web Service > Docker) pointing at the repo; `render.yaml` is included. In the service's environment settings add `APFLOW_ADMIN_KEY` (a long random string) and, optionally, `ANTHROPIC_API_KEY`. To edit thresholds, open `https://YOUR-APP.onrender.com/?admin=YOUR_KEY` and go to the Rules tab. Visitors without the key see the rules read-only.

Free tiers sleep after inactivity, so click through one sample to wake it.

## Layout

```
app/main.py       FastAPI: upload, live events (SSE), sessions, review, settings, evaluation
app/pipeline.py   the eight stages and the decision logic
app/extract.py    reading, OCR, rule-based parsing, LLM gap-fill and cross-check
app/db.py         SQLite: per-visitor runs and ledger, resolutions, settings
app/config.py     default thresholds and flag labels
data/             vendors.json, pos.csv, seed_ledger.json
samples/          generated test invoices + manifest.json
static/index.html the interface (process, dashboard, rules, evaluation)
tools/            make_samples.py, selftest.py, apitest.py, llmtest.py, record_demo.py
```
