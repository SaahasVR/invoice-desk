"""Run every sample through the pipeline against a throwaway DB and compare to the expected decision.

  python3 tools/selftest.py [sample_id ...]     human-readable
  python3 tools/selftest.py --json              machine-readable (used by the app's Evaluation tab)

NOTE: the samples are the cases the rules were developed against, so this is a regression check,
not an independent accuracy measurement.
"""
import json
import os
import sys
import tempfile

if "APFLOW_DB" not in os.environ:
    os.environ["APFLOW_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.setdefault("APFLOW_STAGE_DELAY", "0")
os.environ.pop("ANTHROPIC_API_KEY", None)  # keep the check deterministic and offline
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, db, pipeline  # noqa: E402

SID = "selftest"


def run_one(s):
    ev = []
    return pipeline.Run(os.path.join(config.SAMPLES_DIR, s["file"]), s["file"], ev.append, s["id"], SID).run()


if __name__ == "__main__":
    as_json = "--json" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    db.init()
    if os.environ.get("APFLOW_EVAL_SETTINGS"):
        db.save_settings(json.loads(os.environ["APFLOW_EVAL_SETTINGS"]))
    db.reset_session(SID)
    man = json.load(open(os.path.join(config.SAMPLES_DIR, "manifest.json")))
    rows, bad = [], 0
    for s in man:
        if only and s["id"] not in only:
            continue
        r = run_one(s)
        d = r["decision"]
        ok = d["status"] == s["expected"]
        bad += (not ok)
        rows.append({"id": s["id"], "title": s["title"], "tag": s["tag"], "expected": s["expected"], "got": d["status"],
                     "ok": ok, "headline": d["headline"], "ms": r.get("duration_ms", 0)})
        if not as_json:
            print(f'{"OK " if ok else "BAD"} {s["id"]:28} expected={s["expected"]:13} got={d["status"]:13} | {d["headline"]}')
            if only or not ok:
                for k, v in r["checks"].items():
                    for c in v:
                        print(f'      [{k}] {c["state"]:5} {c["name"]}: {c["detail"]}')
    if as_json:
        print(json.dumps(rows))
    else:
        print("\nfailures:", bad)
    sys.exit(1 if bad else 0)
