"""Record a silent, captioned screen walkthrough of the running app (Playwright video -> webm)."""
import glob
import os
import shutil
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
for f in glob.glob(os.path.join(OUT, "*.webm")):
    os.remove(f)

CAPTION_JS = """
(() => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:22px;transform:translateX(-50%);max-width:1000px;z-index:99999;' +
      'background:rgba(22,38,46,.94);color:#fff;font:600 19px/1.35 system-ui,sans-serif;padding:12px 22px;border-radius:10px;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.3);text-align:center;transition:opacity .25s;pointer-events:none';
    document.body.appendChild(el);
  }
  window.__setCap = t => { el.textContent = t; el.style.opacity = t ? 1 : 0; };
})();
"""


def cap(pg, text, hold=0.0):
    pg.evaluate("t => window.__setCap(t)", text)
    if hold:
        time.sleep(hold)


def run(pg, sample_id, caption, after=3.2):
    cap(pg, caption)
    pg.click(f'.item[data-id="{sample_id}"]')
    pg.wait_for_selector(".stamp", timeout=30000)
    time.sleep(after)
    pg.wait_for_function("!document.querySelector('.item').disabled", timeout=30000)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 860}, record_video_dir=OUT,
                        record_video_size={"width": 1440, "height": 860})
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:8000/")
    pg.wait_for_selector(".item")
    pg.evaluate(CAPTION_JS)
    pg.once("dialog", lambda d: d.accept())
    pg.click("#resetBtn")
    time.sleep(0.6)

    cap(pg, "Invoice desk: an invoice goes in, a decision with its reasoning comes out", 3.5)
    cap(pg, "Left: invoice inbox.  Middle: live audit trail.  Right: the decision", 3.2)

    run(pg, "01_happy_path", "Happy path: clean invoice, exact PO match. Watch each check run.", after=4.5)
    cap(pg, "The original PDF is one click away, next to the reasons", 0.5)
    pg.evaluate("document.querySelector('#resultBody details:last-of-type summary')?.click()")
    time.sleep(2.8)

    run(pg, "03_duplicate_altered_number",
        "Edge case: INV-2041A re-sends the paid INV-2041. Same vendor, amount and date.", after=5.0)
    cap(pg, "Held as a possible duplicate. A plain exact-match check would have missed it.", 3.0)

    run(pg, "04a_split_po_ok", "Edge case: split PO. Second milestone invoice, $4,000 of $12,000.", after=3.2)
    run(pg, "04b_split_po_overrun", "Next milestone would push total billing past the PO, so it goes to a person.", after=3.5)

    run(pg, "10_credit_note", "Edge case: a credit note. It must never be paid, and it links to the invoice it credits.", after=4.5)

    run(pg, "02b_over_tolerance", "8% over the PO: beyond tolerance, so a person decides.", after=2.5)
    cap(pg, "The reviewer adds a note and makes the call. It is logged and the ledger updates.", 0.4)
    pg.type("#revNote", "Vendor confirmed the expedite fee", delay=45)
    time.sleep(1.0)
    pg.click('.review .btn[data-act="approve"]')
    time.sleep(3.0)

    run(pg, "11_rough_scan", "Poor phone photo: OCR is under half confident, so it asks a person instead of guessing.", after=5.0)
    run(pg, "08_unapproved_vendor", "Unknown vendor quoting a real PO number: rejected.", after=3.2)

    cap(pg, "Dashboard: every run, who resolved it, and why invoices get flagged", 0.5)
    pg.click("#tab-dash")
    pg.wait_for_selector("tr.row")
    time.sleep(4.5)
    cap(pg, "Rules: every decision rule in plain English, with the live thresholds", 0.5)
    pg.click("#tab-rules")
    time.sleep(4.5)
    cap(pg, "Evaluation: all built-in invoices re-run in a clean sandbox, expected vs actual", 0.5)
    pg.click("#tab-eval")
    pg.click("#evalBtn")
    pg.wait_for_selector(".evalsum", timeout=60000)
    time.sleep(4.0)
    cap(pg, "", 0.5)
    ctx.close()
    webm = glob.glob(os.path.join(OUT, "*.webm"))[0]
    shutil.move(webm, os.path.join(OUT, "demo.webm"))
    b.close()
print("recorded", os.path.join(OUT, "demo.webm"))
