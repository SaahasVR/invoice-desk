"""FastAPI server: upload/sample -> background pipeline run -> live events over SSE -> history, review, rules, evaluation.

Each visitor gets a cookie that scopes their runs and ledger, so people reviewing the same public link
never interfere with each other (e.g. one person's approved invoice turning another's into a "duplicate").
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, extract, pipeline

app = FastAPI(title="AP Invoice Agent")
UPLOAD_DIR = os.path.join(config.BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
RUNS = {}  # run_id -> {"events": [...], "done": bool}   (live event buffers; history lives in SQLite)
MAX_BYTES = 10 * 1024 * 1024
EVAL_LOCK = threading.Lock()


@app.on_event("startup")
def _startup():
    db.init()


def get_sid(request: Request, response: Response):
    sid = request.cookies.get("apf_sid")
    if not sid or not re.fullmatch(r"[a-f0-9]{24}", sid):
        sid = uuid.uuid4().hex[:24]
        response.set_cookie("apf_sid", sid, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    db.ensure_session(sid)
    return sid


def require_admin(x_admin_key: str = Header(None)):
    if not config.ADMIN_KEY:
        raise HTTPException(403, "Editing is disabled: the server has no APFLOW_ADMIN_KEY set.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, config.ADMIN_KEY):
        raise HTTPException(403, "Admin key required to change rules.")


def _manifest():
    with open(os.path.join(config.SAMPLES_DIR, "manifest.json")) as f:
        return json.load(f)


def _worker(sid, run_id, path, filename):
    state = RUNS[run_id]
    emit = state["events"].append
    t0 = time.time()
    try:
        result = pipeline.Run(path, filename, emit, run_id, sid).run()
    except Exception as e:  # never drop an invoice silently: failures route to a human
        result = {
            "decision": {"status": "NEEDS_REVIEW", "confidence": "low", "po_number": None, "vendor_id": None, "kind": "invoice",
                         "headline": "Needs human review: the process hit an error and could not finish",
                         "reasons": [f"Processing error: {type(e).__name__}: {str(e)[:200]}",
                                     "Nothing was approved or paid automatically."],
                         "actions": ["Route to AP lead for manual handling."], "message": None},
            "extracted": {}, "checks": {}, "findings": [], "amounts": None, "filename": filename}
        emit({"type": "stage", "stage": "decide", "label": "Decide", "state": "fail",
              "summary": result["decision"]["headline"], "checks": [], "data": {}})
        emit({"type": "decision", "result": result})
    dur = int((time.time() - t0) * 1000)
    result["duration_ms"] = dur
    db.save_run(sid, run_id, filename, result, state["events"], dur)
    state["done"] = True


@app.post("/api/run")
async def start_run(file: UploadFile = File(None), sample_id: str = Form(None), sid: str = Depends(get_sid)):
    run_id = uuid.uuid4().hex[:16]
    if sample_id:
        s = next((x for x in _manifest() if x["id"] == sample_id), None)
        if not s:
            raise HTTPException(404, "Unknown sample")
        path = os.path.join(UPLOAD_DIR, f"{run_id}.pdf")
        shutil.copy(os.path.join(config.SAMPLES_DIR, s["file"]), path)
        filename = s["file"]
    elif file is not None:
        data = await file.read()
        if not (file.filename or "").lower().endswith(".pdf") or not data.startswith(b"%PDF"):
            raise HTTPException(400, "Upload a PDF file.")
        if len(data) > MAX_BYTES:
            raise HTTPException(400, "File is larger than 10 MB.")
        path = os.path.join(UPLOAD_DIR, f"{run_id}.pdf")
        with open(path, "wb") as f:
            f.write(data)
        filename = re.sub(r"[^A-Za-z0-9._ -]", "_", file.filename)[:120]
    else:
        raise HTTPException(400, "Provide a PDF or a sample_id.")
    if len(RUNS) > 300:  # keep memory bounded: drop the oldest finished buffers
        for k in [k for k, v in RUNS.items() if v["done"]][:100]:
            RUNS.pop(k, None)
    RUNS[run_id] = {"events": [], "done": False}
    threading.Thread(target=_worker, args=(sid, run_id, path, filename), daemon=True).start()
    return {"run_id": run_id, "filename": filename}


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str):
    state = RUNS.get(run_id)
    if not state:
        raise HTTPException(404, "Run not found (or the server restarted).")

    def gen():
        sent = 0
        while True:
            evs = state["events"]
            while sent < len(evs):
                yield f"data: {json.dumps(evs[sent])}\n\n"
                sent += 1
            if state["done"] and sent >= len(state["events"]):
                yield 'data: {"type":"done"}\n\n'
                return
            time.sleep(0.08)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str):
    p = os.path.join(UPLOAD_DIR, f"{run_id}.pdf")
    if not run_id.isalnum() or not os.path.exists(p):
        raise HTTPException(404, "File not found")
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/runs")
def runs(sid: str = Depends(get_sid)):
    return {"runs": db.list_runs(sid), "stats": db.stats(sid)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, sid: str = Depends(get_sid)):
    r = db.get_run(sid, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    return r


class ReviewIn(BaseModel):
    action: str
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: ReviewIn, sid: str = Depends(get_sid)):
    r = db.get_run(sid, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if r["decision"] not in ("NEEDS_REVIEW", "ON_HOLD"):
        raise HTTPException(400, "Only items waiting for a person can be resolved.")
    if r["resolution"]:
        raise HTTPException(409, "This item has already been resolved.")
    if body.action not in db.RESOLUTIONS:
        raise HTTPException(400, "Action must be approve, reject or request_info.")
    db.resolve_run(sid, run_id, body.action, body.note.strip()[:500])
    return {"ok": True, "resolution": db.RESOLUTIONS[body.action]}


@app.get("/api/settings")
def get_settings():
    return {"values": db.get_settings(), "defaults": config.DEFAULT_SETTINGS}


class SettingsIn(BaseModel):
    auto_approve_pct: float
    auto_approve_abs: float
    review_pct: float
    duplicate_window_days: int
    ocr_min_confidence: float
    manual_minutes: float


@app.post("/api/settings", dependencies=[Depends(require_admin)])
def set_settings(s: SettingsIn):
    v = s.model_dump()
    if not (0 <= v["auto_approve_pct"] <= v["review_pct"] <= 1):
        raise HTTPException(400, "Auto-approve % must be between 0 and the review %, and the review % must be at most 100%.")
    if v["auto_approve_abs"] < 0 or not (0 <= v["duplicate_window_days"] <= 90):
        raise HTTPException(400, "Auto-approve $ must be 0 or more, and the duplicate window 0-90 days.")
    if not (0 <= v["ocr_min_confidence"] <= 1) or not (0 <= v["manual_minutes"] <= 240):
        raise HTTPException(400, "OCR confidence must be 0-100%, and manual minutes 0-240.")
    db.save_settings(v)
    return {"values": db.get_settings()}


@app.post("/api/settings/reset", dependencies=[Depends(require_admin)])
def settings_reset():
    db.reset_settings()
    return {"values": db.get_settings()}


@app.get("/api/samples")
def samples():
    return _manifest()


@app.post("/api/reset")
def reset(sid: str = Depends(get_sid)):
    """Clears only the calling visitor's history and restores their starting ledger."""
    db.reset_session(sid)
    return {"ok": True}


@app.post("/api/eval")
def evaluate():
    """Run every built-in sample in an isolated throwaway database (rules-only, so it is repeatable)."""
    if not EVAL_LOCK.acquire(blocking=False):
        raise HTTPException(429, "An evaluation is already running.")
    try:
        tmp = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env.update({"APFLOW_DB": os.path.join(tmp, "eval.db"), "APFLOW_STAGE_DELAY": "0",
                    "APFLOW_EVAL_SETTINGS": json.dumps(db.get_settings())})
        p = subprocess.run([sys.executable, "-I", os.path.join(config.BASE_DIR, "tools", "selftest.py"), "--json"],
                           capture_output=True, text=True, timeout=180, env=env, cwd=config.BASE_DIR)
        if p.returncode not in (0, 1):
            raise HTTPException(500, "Evaluation failed: " + (p.stderr or p.stdout)[-300:])
        rows = json.loads(p.stdout)
        n, ok = len(rows), sum(1 for r in rows if r["ok"])
        stp = sum(1 for r in rows if r["got"] == "APPROVED")
        return {"rows": rows, "total": n, "matched": ok, "approved_auto": stp,
                "straight_through_pct": round(100 * stp / n) if n else 0,
                "thresholds": db.get_settings()}
    finally:
        EVAL_LOCK.release()
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/api/info")
def info(sid: str = Depends(get_sid)):
    return {"llm_fallback": extract.llm_available(), "crosscheck": extract.llm_available() and config.CROSSCHECK,
            "model": config.ANTHROPIC_MODEL}


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})


app.mount("/", StaticFiles(directory=os.path.join(config.BASE_DIR, "static"), html=True), name="static")
