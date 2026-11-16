"""FastAPI adapter for Vercel with Supabase."""

import os
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

# --- VERCEL REQUIREMENT ---
app = FastAPI()

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "aimodels" / "web"
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
TOKEN = os.environ.get("DASHBOARD_TOKEN", "vercel-token")
ALLOWED_STATIC = {"app.css", "app.js", "charts.js"}
SQL_ROW_LIMIT = 1000

def db():
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise HTTPException(500, "SUPABASE_DB_URL is not set in Vercel Environment Variables")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn

def rows_of(cursor) -> list[dict]:
    cols = [d[0] for d in (cursor.description or [])]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]

# ---------- HTML & static ----------

@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__TOKEN__", TOKEN))

@app.get("/static/{name}")
def static_file(name: str):
    if name not in ALLOWED_STATIC:
        raise HTTPException(404, "not found")
    p = WEB_DIR / name
    if not p.exists():
        raise HTTPException(404, "not found")
    media_type = "application/javascript" if name.endswith(".js") else "text/css"
    return Response(p.read_bytes(), media_type=media_type)

@app.get("/favicon.ico")
def favicon():
    return Response(b"", media_type="image/x-icon", status_code=204)

# ---------- read API ----------

@app.get("/api/status")
def status():
    conn = db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) AS n FROM models")
        n = cur.fetchone()["n"]
        cur.execute("SELECT run_id, finished_at, source, n_valid, n_rejected FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
        last = cur.fetchone()
        return {
            "datasets": {
                "live": {
                    "exists": True, "models": n,
                    "last_run": last if last else None,
                    "has_report": (REPORTS_DIR / "eda_summary.md").exists(),
                },
                "sample": {"exists": False, "models": 0, "last_run": None, "has_report": False},
            },
            "keys": {"aa": False, "hf": False},
            "cached_raw": [], "job": None,
            "defaults": {"fuzzy_threshold": 0.93, "max_reject_fraction": 0.25},
            "attribution": "Data: Artificial Analysis (artificialanalysis.ai), OpenRouter, Hugging Face Hub",
            "stages": ["extract", "transform", "validate", "load", "eda"],
        }
    finally:
        conn.close()

@app.get("/api/models")
def models():
    conn = db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM models ORDER BY name")
        return {"dataset": "live", "run": None, "models": [dict(r) for r in cur.fetchall()]}
    finally:
        conn.close()

@app.get("/api/quality")
def quality():
    conn = db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        def q(sql):
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]

        return {
            "rejects": q("SELECT model_key, name, errors FROM rejects WHERE run_id = (SELECT MAX(run_id) FROM pipeline_runs)"),
            "runs": q("SELECT run_id, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected FROM pipeline_runs ORDER BY run_id DESC LIMIT 20"),
            "hf_status": q("SELECT status, COUNT(*) AS n FROM raw_huggingface GROUP BY status"),
            "snapshots": q("SELECT snapshot_date AS date, COUNT(*) AS n, AVG(intelligence_index) AS avg_score, AVG(price_blended_usd_per_m) AS avg_price FROM model_snapshots GROUP BY snapshot_date ORDER BY 1"),
            "unmatched": q("SELECT name, name_key FROM models WHERE match_method = 'none' AND hf_id IS NULL ORDER BY name LIMIT 300"),
            "manual_hf": [],
        }
    finally:
        conn.close()

@app.get("/api/report")
def report():
    md = REPORTS_DIR / "eda_summary.md"
    fig_dir = REPORTS_DIR / "figures"
    figs = sorted(fig_dir.glob("*.png")) if fig_dir.exists() else []
    return {
        "exists": md.exists(),
        "markdown": md.read_text(encoding="utf-8") if md.exists() else "",
        "mtime": md.stat().st_mtime if md.exists() else None,
        "figures": [{"name": f.name, "url": f"/reports/figures/{f.name}?v={int(f.stat().st_mtime)}"} for f in figs],
    }

@app.get("/reports/figures/{name}")
def figure(name: str):
    p = REPORTS_DIR / "figures" / name
    if not p.exists() or p.suffix != ".png":
        raise HTTPException(404, "not found")
    return Response(p.read_bytes(), media_type="image/png")

# ---------- SQL console ----------

@app.post("/api/sql")
async def sql(request: Request):
    body = await request.json()
    query = str(body.get("query", "")).strip()
    if not query.lower().startswith(("select", "with")):
        raise HTTPException(400, "Only SELECT queries are allowed")
    conn = db()
    try:
        started = time.time()
        cur = conn.cursor()
        cur.execute(query)
        cols = [d[0] for d in (cur.description or [])]
        data = cur.fetchmany(SQL_ROW_LIMIT + 1)
        truncated = len(data) > SQL_ROW_LIMIT
        data = [list(r) for r in data[:SQL_ROW_LIMIT]]
        return {"columns": cols, "rows": data, "truncated": truncated, "ms": round((time.time() - started) * 1000)}
    finally:
        conn.close()

# ---------- ETL is disabled on Vercel ----------

_ETL_MSG = "Vercel Deployment: ETL must be run locally or via a cron job. Data is synced to Supabase."

@app.post("/api/run")
async def run_disabled():
    return JSONResponse(status_code=200, content={
        "id": "vercel-disabled", "kind": "etl", "status": "done", "stage": "done",
        "error": None, 
        "lines": [{"t": time.strftime("%H:%M:%S"), "lvl": "INFO", "msg": _ETL_MSG}],
        "result": {"n_valid": 0, "n_rejected": 0}, 
        "done_stages": ["extract", "transform", "validate", "load", "eda"],
        "params": {}, "started": time.time(), "finished": time.time(), "next": 1,
    })

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return {
        "id": job_id, "kind": "etl", "status": "done", "stage": "done",
        "error": None, 
        "lines": [{"t": time.strftime("%H:%M:%S"), "lvl": "INFO", "msg": _ETL_MSG}],
        "result": {"n_valid": 0, "n_rejected": 0}, 
        "done_stages": ["extract", "transform", "validate", "load", "eda"],
        "params": {}, "started": time.time(), "finished": time.time(), "next": 1,
    }

@app.post("/api/keys")
async def keys_disabled():
    return {"aa": False, "hf": False}

@app.post("/api/diagnose")
async def diagnose_disabled():
    return {"results": [{"source": "Vercel", "ok": True, "message": "Read-only mode. ETL runs externally."}]}

@app.post("/api/manual_hf")
async def manual_hf_disabled():
    raise HTTPException(503, "Manual Hugging Face edits are disabled on Vercel.")
