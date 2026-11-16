"""FastAPI adapter for Vercel with Supabase (Live) and SQLite (Demo)."""

import os
import sqlite3
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

app = FastAPI()

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "aimodels" / "web"
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
SAMPLE_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "sample.db"
TOKEN = os.environ.get("DASHBOARD_TOKEN", "vercel-token")
ALLOWED_STATIC = {"app.css", "app.js", "charts.js"}
SQL_ROW_LIMIT = 1000


def get_db(which: str):
    """Returns a connection to either Supabase (live) or SQLite (sample)."""
    if which == "sample":
        if not SAMPLE_DB_PATH.exists():
            raise HTTPException(404, "Demo database not found. Please run `python -m aimodels all --offline` and push sample.db.")
        conn = sqlite3.connect(f"file:{SAMPLE_DB_PATH.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    else:
        url = os.environ.get("SUPABASE_DB_URL")
        if not url:
            raise HTTPException(500, "SUPABASE_DB_URL is not set in Vercel Environment Variables")
        conn = psycopg2.connect(url)
        conn.autocommit = True
        return conn


def q(conn, sql: str) -> list[dict]:
    """Helper to execute a query and return a list of dictionaries for both DBs."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if not isinstance(conn, sqlite3.Connection) else conn.cursor()
    cur.execute(sql)
    if isinstance(conn, sqlite3.Connection):
        cols = [d[0] for d in (cur.description or [])]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    else:
        return [dict(r) for r in cur.fetchall()]


# ---------- HTML & static ----------

@app.get("/", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
@app.get("/api/", response_class=HTMLResponse)
@app.get("/api/index", response_class=HTMLResponse)
@app.get("/api/index/", response_class=HTMLResponse)
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
    datasets = {}
    for which in ("live", "sample"):
        try:
            conn = get_db(which)
            try:
                n = q(conn, "SELECT COUNT(*) AS n FROM models")[0]["n"]
                last = q(conn, "SELECT run_id, finished_at, source, n_valid, n_rejected FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
                report_dir = REPORTS_DIR / "sample" if which == "sample" else REPORTS_DIR
                datasets[which] = {
                    "exists": True, "models": n, "last_run": last[0] if last else None,
                    "has_report": (report_dir / "eda_summary.md").exists(),
                }
            finally:
                conn.close()
        except Exception:
            datasets[which] = {"exists": False, "models": 0, "last_run": None, "has_report": False}

    return {
        "datasets": datasets,
        "keys": {"aa": bool(os.environ.get("AA_API_KEY")), "hf": bool(os.environ.get("HF_TOKEN"))},
        "cached_raw": [], "job": None,
        "defaults": {"fuzzy_threshold": 0.93, "max_reject_fraction": 0.25},
        "attribution": "Data: Artificial Analysis (artificialanalysis.ai), OpenRouter, Hugging Face Hub",
        "stages": ["extract", "transform", "validate", "load", "eda"],
    }

@app.get("/api/models")
def models(db: str = "sample"):
    conn = get_db(db)
    try:
        return {"dataset": db, "run": None, "models": q(conn, "SELECT * FROM models ORDER BY name")}
    finally:
        conn.close()

@app.get("/api/quality")
def quality(db: str = "sample"):
    conn = get_db(db)
    try:
        return {
            "rejects": q(conn, "SELECT model_key, name, errors FROM rejects WHERE run_id = (SELECT MAX(run_id) FROM pipeline_runs)"),
            "runs": q(conn, "SELECT run_id, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected FROM pipeline_runs ORDER BY run_id DESC LIMIT 20"),
            "hf_status": q(conn, "SELECT status, COUNT(*) AS n FROM raw_huggingface GROUP BY status"),
            "snapshots": q(conn, "SELECT snapshot_date AS date, COUNT(*) AS n, AVG(intelligence_index) AS avg_score, AVG(price_blended_usd_per_m) AS avg_price FROM model_snapshots GROUP BY snapshot_date ORDER BY 1"),
            "unmatched": q(conn, "SELECT name, name_key FROM models WHERE match_method = 'none' AND hf_id IS NULL ORDER BY name LIMIT 300"),
            "manual_hf": [],
        }
    finally:
        conn.close()

@app.get("/api/report")
def report(db: str = "sample"):
    report_dir = REPORTS_DIR / "sample" if db == "sample" else REPORTS_DIR
    md = report_dir / "eda_summary.md"
    fig_dir = report_dir / "figures"
    figs = sorted(fig_dir.glob("*.png")) if fig_dir.exists() else []
    
    prefix = "/reports/sample/figures/" if db == "sample" else "/reports/figures/"
    return {
        "exists": md.exists(),
        "markdown": md.read_text(encoding="utf-8") if md.exists() else "",
        "mtime": md.stat().st_mtime if md.exists() else None,
        "figures": [{"name": f.name, "url": f"{prefix}{f.name}?v={int(f.stat().st_mtime)}"} for f in figs],
    }

@app.get("/reports/figures/{name}")
@app.get("/reports/sample/figures/{name}")
def figure(name: str, request: Request):
    is_sample = "/sample/" in request.url.path
    fig_dir = (REPORTS_DIR / "sample" / "figures") if is_sample else (REPORTS_DIR / "figures")
    p = fig_dir / name
    if not p.exists() or p.suffix != ".png":
        raise HTTPException(404, "not found")
    return Response(p.read_bytes(), media_type="image/png")

# ---------- SQL console ----------

@app.post("/api/sql")
async def sql(request: Request):
    body = await request.json()
    db_name = str(body.get("dataset", "sample"))
    query = str(body.get("query", "")).strip()
    if not query.lower().startswith(("select", "with")):
        raise HTTPException(400, "Only SELECT queries are allowed")
    conn = get_db(db_name)
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

_ETL_MSG = "Vercel Deployment: ETL is handled externally. Run `python -m aimodels all` locally to sync new data."

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
    return {"aa": bool(os.environ.get("AA_API_KEY")), "hf": bool(os.environ.get("HF_TOKEN"))}

@app.post("/api/diagnose")
async def diagnose_disabled():
    return {"results": [{"source": "Vercel", "ok": True, "message": "Read-only mode. ETL runs externally."}]}

@app.post("/api/manual_hf")
async def manual_hf_disabled():
    raise HTTPException(503, "Manual Hugging Face edits are disabled on Vercel.")
