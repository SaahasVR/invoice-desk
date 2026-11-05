"""SQLite persistence.

* runs      - history for the dashboard (one row per processed invoice), plus the human resolution
* ledger    - the payment ledger that duplicate detection and split-PO balance tracking read from
* sessions  - every visitor gets their own isolated runs + ledger, seeded fresh, so two people
              reviewing the same public link never see each other's invoices (or fake "duplicates")
* settings  - tunable thresholds (global, admin-editable)
"""
import csv
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()
# ledger rows in these states are ignored by duplicate checks (superseded or voided)
INACTIVE = ("VOID", "INFO_REQUESTED")


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with _lock, conn() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(runs)")]
        if cols and "sid" not in cols:  # database from an older version: demo data only, safe to rebuild
            c.executescript("DROP TABLE IF EXISTS runs; DROP TABLE IF EXISTS ledger;")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, sid TEXT, created_at TEXT, filename TEXT, status TEXT, decision TEXT,
            vendor TEXT, invoice_number TEXT, total REAL, po_number TEXT, headline TEXT,
            duration_ms INTEGER, result_json TEXT, events_json TEXT,
            resolution TEXT, resolution_note TEXT, resolved_at TEXT);
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT, vendor_id TEXT, invoice_number TEXT, invoice_date TEXT,
            total REAL, po_number TEXT, status TEXT, source TEXT, run_id TEXT, created_at TEXT);
        CREATE INDEX IF NOT EXISTS ix_runs_sid ON runs(sid, created_at);
        CREATE INDEX IF NOT EXISTS ix_ledger_sid ON ledger(sid, vendor_id);
        """)
        # housekeeping: forget visitors idle for 14+ days
        c.execute("DELETE FROM runs WHERE sid IN (SELECT sid FROM sessions WHERE created_at < datetime('now','-14 day'))")
        c.execute("DELETE FROM ledger WHERE sid IN (SELECT sid FROM sessions WHERE created_at < datetime('now','-14 day'))")
        c.execute("DELETE FROM sessions WHERE created_at < datetime('now','-14 day')")


# ---- sessions
def _seed(c, sid):
    with open(os.path.join(config.DATA_DIR, "seed_ledger.json")) as f:
        for r in json.load(f):
            c.execute("INSERT INTO ledger(sid,vendor_id,invoice_number,invoice_date,total,po_number,status,source,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (sid, r["vendor_id"], r["invoice_number"], r["invoice_date"], r["total"], r["po_number"],
                       r["status"], "seed", now()))


def ensure_session(sid):
    with _lock, conn() as c:
        if c.execute("SELECT 1 FROM sessions WHERE sid=?", (sid,)).fetchone():
            return
        c.execute("INSERT INTO sessions(sid,created_at) VALUES(?,?)", (sid, now()))
        _seed(c, sid)


def reset_session(sid):
    """Clear this visitor's history and restore their seeded ledger so demos are repeatable."""
    with _lock, conn() as c:
        c.execute("DELETE FROM runs WHERE sid=?", (sid,))
        c.execute("DELETE FROM ledger WHERE sid=?", (sid,))
        c.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        c.execute("INSERT INTO sessions(sid,created_at) VALUES(?,?)", (sid, now()))
        _seed(c, sid)


# ---- settings
def get_settings():
    out = dict(config.DEFAULT_SETTINGS)
    with conn() as c:
        for r in c.execute("SELECT key,value FROM settings"):
            if r["key"] in out:
                try:
                    out[r["key"]] = type(config.DEFAULT_SETTINGS[r["key"]])(float(r["value"]))
                except ValueError:
                    pass
    return out


def save_settings(values):
    with _lock, conn() as c:
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, str(v)))


def reset_settings():
    with _lock, conn() as c:
        c.execute("DELETE FROM settings")


# ---- reference data (loaded fresh so edits to the files take effect without a restart)
def load_vendors():
    with open(os.path.join(config.DATA_DIR, "vendors.json")) as f:
        return json.load(f)


def load_pos():
    with open(os.path.join(config.DATA_DIR, "pos.csv")) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["net_amount"] = float(r["net_amount"])
        r["tax_rate"] = float(r["tax_rate"])
        r["gross_amount"] = round(r["net_amount"] * (1 + r["tax_rate"]), 2)
    return rows


# ---- ledger
def ledger_for_vendor(sid, vendor_id):
    """Everything on file for this vendor that can still be a duplicate of a new invoice:
    paid, approved, pending review and held. Voided / superseded rows are skipped."""
    with conn() as c:
        q = "SELECT * FROM ledger WHERE sid=? AND vendor_id=? AND status NOT IN (%s)" % ",".join("?" * len(INACTIVE))
        return [dict(r) for r in c.execute(q, (sid, vendor_id, *INACTIVE))]


def billed_against_po(sid, po_number):
    with conn() as c:
        rows = c.execute("SELECT invoice_number,total,status,invoice_date FROM ledger WHERE sid=? AND po_number=? "
                         "AND status IN ('PAID','APPROVED')", (sid, po_number)).fetchall()
    return [dict(r) for r in rows]


def add_ledger(sid, vendor_id, invoice_number, invoice_date, total, po_number, run_id, status="APPROVED"):
    with _lock, conn() as c:
        c.execute("INSERT INTO ledger(sid,vendor_id,invoice_number,invoice_date,total,po_number,status,source,run_id,created_at) "
                  "VALUES(?,?,?,?,?,?,?,'run',?,?)",
                  (sid, vendor_id, invoice_number, invoice_date, total, po_number, status, run_id, now()))


def set_ledger_status(sid, run_id, status):
    with _lock, conn() as c:
        c.execute("UPDATE ledger SET status=? WHERE sid=? AND run_id=?", (status, sid, run_id))


# ---- runs
def save_run(sid, run_id, filename, result, events, duration_ms):
    d = result["decision"]
    ex = result.get("extracted", {})
    with _lock, conn() as c:
        c.execute("INSERT OR REPLACE INTO runs(id,sid,created_at,filename,status,decision,vendor,invoice_number,total,po_number,"
                  "headline,duration_ms,result_json,events_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (run_id, sid, now(), filename, "done", d["status"], ex.get("vendor_name"), ex.get("invoice_number"),
                   ex.get("total"), d.get("po_number"), d["headline"], duration_ms,
                   json.dumps(result), json.dumps(events)))


def list_runs(sid, limit=300):
    with conn() as c:
        rows = c.execute("SELECT id,created_at,filename,decision,vendor,invoice_number,total,po_number,headline,duration_ms,"
                         "resolution,resolution_note,resolved_at FROM runs WHERE sid=? "
                         "ORDER BY created_at DESC, rowid DESC LIMIT ?", (sid, limit)).fetchall()
    return [dict(r) for r in rows]


def get_run(sid, run_id):
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=? AND sid=?", (run_id, sid)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["result"] = json.loads(d.pop("result_json"))
    d["events"] = json.loads(d.pop("events_json"))
    return d


RESOLUTIONS = {"approve": "APPROVED", "reject": "REJECTED", "request_info": "INFO_REQUESTED"}
LEDGER_AFTER = {"approve": "APPROVED", "reject": "VOID", "request_info": "INFO_REQUESTED"}


def resolve_run(sid, run_id, action, note):
    with _lock, conn() as c:
        c.execute("UPDATE runs SET resolution=?, resolution_note=?, resolved_at=? WHERE id=? AND sid=?",
                  (RESOLUTIONS[action], note, now(), run_id, sid))
        c.execute("UPDATE ledger SET status=? WHERE sid=? AND run_id=?", (LEDGER_AFTER[action], sid, run_id))


def stats(sid):
    with conn() as c:
        rows = c.execute("SELECT decision,resolution,total,duration_ms,result_json FROM runs WHERE sid=?", (sid,)).fetchall()
    cfg = get_settings()
    by, flags = {}, {}
    auto = pending = resolved = 0
    approved_amt = 0.0
    ms = 0
    for r in rows:
        res = json.loads(r["result_json"])
        kind = res["decision"].get("kind")
        b = by.setdefault(r["decision"], {"count": 0, "amount": 0.0})
        b["count"] += 1
        b["amount"] = round(b["amount"] + (r["total"] or 0), 2)
        ms += r["duration_ms"] or 0
        if r["decision"] == "APPROVED":
            auto += 1
        if r["decision"] in ("NEEDS_REVIEW", "ON_HOLD"):
            if r["resolution"]:
                resolved += 1
            else:
                pending += 1
        if kind != "credit_note" and (r["decision"] == "APPROVED" or r["resolution"] == "APPROVED"):
            approved_amt += r["total"] or 0
        for code in {f["code"] for f in res.get("findings", []) if f["severity"] != "info"}:
            flags[code] = flags.get(code, 0) + 1
    top = sorted(flags.items(), key=lambda kv: -kv[1])[:6]
    n = len(rows)
    return {"total": n, "by_decision": by, "auto_approved": auto, "pending": pending, "resolved": resolved,
            "approved_amount": round(approved_amt, 2), "avg_ms": int(ms / n) if n else 0,
            "flags": [{"code": k, "label": config.FLAG_LABELS.get(k, k), "count": v} for k, v in top],
            "manual_minutes": cfg["manual_minutes"], "minutes_saved": round(auto * cfg["manual_minutes"], 1)}
