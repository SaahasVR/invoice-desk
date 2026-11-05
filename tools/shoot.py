"""Headless-browser smoke test + screenshots: runs a sample through the real UI and captures the result."""
import sys, time
from playwright.sync_api import sync_playwright
OUT = sys.argv[1]
SAMPLE = sys.argv[2] if len(sys.argv) > 2 else "03_duplicate_altered_number"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1480, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8000/")
    pg.wait_for_selector(".item")
    pg.screenshot(path=f"{OUT}/1_idle.png")
    pg.click(f'.item[data-id="{SAMPLE}"]')
    time.sleep(1.1)
    pg.screenshot(path=f"{OUT}/2_running.png")
    pg.wait_for_selector(".stamp", timeout=20000)
    time.sleep(1.0)
    pg.screenshot(path=f"{OUT}/3_done.png", full_page=True)
    pg.click("#tab-dash")
    pg.wait_for_selector("tr.row")
    pg.screenshot(path=f"{OUT}/4_dash.png", full_page=True)
    print("stamp:", pg.inner_text(".stamp"), "| rows:", pg.locator("tr.row").count(), "| js errors:", errs)
    b.close()
