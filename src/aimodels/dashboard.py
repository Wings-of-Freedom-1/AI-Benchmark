"""Local web dashboard: run and explore the whole pipeline from a browser.

``python -m aimodels dashboard`` (or double-click a launcher) starts a small
server on 127.0.0.1 only, and opens the control surface. Everything the command
line can do is a control on that page: run the ETL (demo or live), tune matching
strictness and reject tolerance, set API keys for the session, rank and filter
models, browse the EDA, inspect rejected rows, edit the Hugging Face map and run
read-only SQL.

Design notes
* Standard library only for serving; pandas / matplotlib are imported lazily by jobs.
* The server never binds beyond localhost, rejects foreign ``Host`` headers (DNS
  rebinding) and requires a per-launch token on every POST and on the SQL console.
* API keys entered in the page live in this process's memory only, never on disk.
* The SQL console opens the database read-only and denies everything but SELECT.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .config import ATTRIBUTION, DB_PATH, FUZZY_THRESHOLD, MANUAL_HF_PATH, MAX_REJECT_FRACTION, RAW_DIR, REPORTS_DIR, SAMPLE_DB_PATH

log = logging.getLogger("aimodels.dashboard")

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 1_000_000
SQL_ROW_LIMIT = 1000
SQL_TIME_LIMIT_S = 5.0
STAGES = ["extract", "transform", "validate", "load", "eda"]
ALLOWED_STATIC = {"app.css", "app.js", "charts.js"}


@dataclass
class Paths:
    """Where the dashboard reads and writes. Tests point these at a temp folder."""

    live_db: Path = DB_PATH
    sample_db: Path = SAMPLE_DB_PATH
    reports: Path = REPORTS_DIR
    manual_hf: Path = MANUAL_HF_PATH
    raw: Path = RAW_DIR

    def db(self, which: str) -> Path:
        return self.sample_db if which == "sample" else self.live_db

    def report_dir(self, which: str) -> Path:
        return self.reports / "sample" if which == "sample" else self.reports


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
class Job:
    def __init__(self, kind: str, params: dict):
        self.id = secrets.token_hex(4)
        self.kind = kind
        self.params = params
        self.status = "queued"  # queued | running | done | error
        self.stage: str | None = None
        self.done_stages: list[str] = []
        self.lines: list[dict] = []
        self.started = time.time()
        self.finished: float | None = None
        self.error: str | None = None
        self.result: dict = {}
        self._lock = threading.Lock()

    def log(self, level: str, msg: str) -> None:
        with self._lock:
            self.lines.append({"t": time.strftime("%H:%M:%S"), "lvl": level, "msg": msg})

    def set_stage(self, name: str) -> None:
        with self._lock:
            if self.stage and self.stage not in self.done_stages:
                self.done_stages.append(self.stage)
            self.stage = name

    def to_dict(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "params": self.params, "status": self.status,
                "stage": self.stage, "done_stages": list(self.done_stages), "error": self.error,
                "started": self.started, "finished": self.finished, "result": self.result,
                "lines": self.lines[since:], "next": len(self.lines),
            }


class _JobHandler(logging.Handler):
    """Copies this thread's log records into the job's console."""

    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job, self.thread_id = job, threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id:
            self.job.log(record.levelname, record.getMessage())


class App:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths()
        self.token = secrets.token_urlsafe(24)
        self.keys: dict[str, str] = {}
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self._lock = threading.Lock()

    # ---- jobs -----------------------------------------------------------
    def start_job(self, kind: str, params: dict) -> Job:
        if kind not in ("etl", "eda", "all"):
            raise ValueError("kind must be etl, eda or all")
        with self._lock:
            if self.current and self.current.status in ("queued", "running"):
                raise RuntimeError("A job is already running.")
            clean = self._clean_params(params)
            job = Job(kind, clean)
            self.jobs[job.id] = job
            self.current = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job

    @staticmethod
    def _clean_params(p: dict) -> dict:
        def num(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return min(max(float(p.get(key, default)), lo), hi)
            except (TypeError, ValueError):
                return default

        return {
            "dataset": "live" if p.get("dataset") == "live" else "sample",
            "use_cache": bool(p.get("use_cache")),
            "skip_hf": bool(p.get("skip_hf")),
            "fuzzy_threshold": num("fuzzy_threshold", FUZZY_THRESHOLD, 0.5, 1.0),
            "max_reject_fraction": num("max_reject_fraction", MAX_REJECT_FRACTION, 0.0, 1.0),
        }

    def _run_job(self, job: Job) -> None:
        job.status = "running"
        logger = logging.getLogger("aimodels")
        handler = _JobHandler(job)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        p = job.params
        try:
            dataset = p["dataset"]
            db = self.paths.db(dataset)
            if self.keys.get("AA_API_KEY"):
                os.environ["AA_API_KEY"] = self.keys["AA_API_KEY"]
            if self.keys.get("HF_TOKEN"):
                os.environ["HF_TOKEN"] = self.keys["HF_TOKEN"]

            if job.kind in ("etl", "all"):
                from .pipeline import run_etl

                summary = run_etl(
                    offline=dataset == "sample", use_cache=p["use_cache"], skip_hf=p["skip_hf"], db_path=db,
                    raw_dir=self.paths.raw, manual_hf_path=self.paths.manual_hf,
                    fuzzy_threshold=p["fuzzy_threshold"], max_reject_fraction=p["max_reject_fraction"],
                    on_stage=job.set_stage,
                )
                job.result.update(run_id=summary.run_id, n_valid=summary.n_valid, n_rejected=summary.n_rejected)
                job.log("INFO", f"ETL finished: {summary.n_valid} models loaded, {summary.n_rejected} rejected")
            if job.kind in ("eda", "all"):
                if not db.exists():
                    raise FileNotFoundError("There is no data yet - run the pipeline first.")
                from .eda import run_eda

                job.set_stage("eda")
                report = run_eda(db, self.paths.report_dir(dataset))
                job.log("INFO", f"EDA finished: {report.name} and figures written")
            job.set_stage("done")
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - surface every failure to the console
            from .extract import ExtractError
            from .schemas import PipelineValidationError

            job.error = str(exc) or exc.__class__.__name__
            if not isinstance(exc, (ExtractError, PipelineValidationError, FileNotFoundError)):
                job.log("DEBUG", traceback.format_exc())
            job.log("ERROR", job.error)
            job.status = "error"
        finally:
            job.finished = time.time()
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

    # ---- read-side helpers ---------------------------------------------
    def _connect_ro(self, which: str) -> sqlite3.Connection | None:
        db = self.paths.db(which)
        if not db.exists():
            return None
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def status(self) -> dict:
        datasets = {}
        for which in ("sample", "live"):
            info = {"exists": False, "models": 0, "last_run": None, "has_report": False}
            conn = self._connect_ro(which)
            if conn:
                try:
                    info["exists"] = True
                    info["models"] = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
                    row = conn.execute("SELECT * FROM pipeline_runs ORDER BY run_id DESC LIMIT 1").fetchone()
                    if row:
                        run = dict(row)
                        run["stats"] = json.loads(run.pop("stats_json") or "{}")
                        info["last_run"] = run
                except sqlite3.Error:
                    info["exists"] = False
                finally:
                    conn.close()
            info["has_report"] = (self.paths.report_dir(which) / "eda_summary.md").exists()
            datasets[which] = info
        raw_files = sorted(p.name for p in self.paths.raw.glob("*.json")) if self.paths.raw.exists() else []
        return {
            "datasets": datasets,
            "keys": {"aa": bool(self.keys.get("AA_API_KEY") or os.environ.get("AA_API_KEY")),
                     "hf": bool(self.keys.get("HF_TOKEN") or os.environ.get("HF_TOKEN"))},
            "cached_raw": [f for f in raw_files if not f.startswith("hf_")],
            "job": self.current.to_dict(since=10**9) if self.current else None,
            "defaults": {"fuzzy_threshold": FUZZY_THRESHOLD, "max_reject_fraction": MAX_REJECT_FRACTION},
            "attribution": ATTRIBUTION,
            "stages": STAGES,
        }

    def diagnose(self) -> list[dict]:
        """Probe the three live sources with the session's keys; never raises."""
        from .extract import check_connections

        aa = self.keys.get("AA_API_KEY") or os.environ.get("AA_API_KEY") or os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
        hf = self.keys.get("HF_TOKEN") or os.environ.get("HF_TOKEN")
        try:
            return check_connections(aa, hf)
        except Exception as exc:  # noqa: BLE001
            return [{"source": "Connection test", "ok": False, "message": f"{exc.__class__.__name__}: {exc}"}]

    def models(self, which: str) -> dict | None:
        conn = self._connect_ro(which)
        if conn is None:
            return None
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM models ORDER BY name")]
            run = conn.execute("SELECT run_id, finished_at, source FROM pipeline_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        return {"dataset": which, "run": dict(run) if run else None, "models": rows}

    def quality(self, which: str) -> dict | None:
        conn = self._connect_ro(which)
        if conn is None:
            return None

        def q(sql: str) -> list[dict]:
            try:
                return [dict(r) for r in conn.execute(sql)]
            except sqlite3.Error:
                return []

        try:
            return {
                "rejects": q("SELECT model_key, name, errors FROM rejects WHERE run_id = (SELECT MAX(run_id) FROM pipeline_runs)"),
                "runs": q("SELECT run_id, finished_at, source, n_aa, n_openrouter, n_hf, n_valid, n_rejected "
                          "FROM pipeline_runs ORDER BY run_id DESC LIMIT 20"),
                "hf_status": q("SELECT status, COUNT(*) AS n FROM raw_huggingface GROUP BY status"),
                "snapshots": q("SELECT snapshot_date AS date, COUNT(*) AS n, AVG(intelligence_index) AS avg_score, "
                               "AVG(price_blended_usd_per_m) AS avg_price FROM model_snapshots GROUP BY snapshot_date ORDER BY 1"),
                "unmatched": q("SELECT name, name_key FROM models WHERE match_method = 'none' AND hf_id IS NULL "
                               "ORDER BY name LIMIT 300"),
                "manual_hf": self.manual_hf(),
            }
        finally:
            conn.close()

    def manual_hf(self) -> list[dict]:
        from .pipeline import load_manual_hf

        return [{"match_key": k, "hf_repo_id": v} for k, v in sorted(load_manual_hf(self.paths.manual_hf).items())]

    def edit_manual_hf(self, op: str, key: str, repo: str = "") -> list[dict]:
        from .pipeline import load_manual_hf, save_manual_hf

        key, repo = key.strip().lower(), repo.strip()
        mapping = load_manual_hf(self.paths.manual_hf)
        if op == "add":
            if not key or not all(c.isalnum() or c == "-" for c in key):
                raise ValueError("match_key may only contain letters, digits and '-'")
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts) or not all(c.isalnum() or c in "-_." for c in repo.replace("/", "")):
                raise ValueError("Hugging Face repo must look like owner/name")
            mapping[key] = repo
        elif op == "remove":
            mapping.pop(key, None)
        else:
            raise ValueError("op must be add or remove")
        save_manual_hf(mapping, self.paths.manual_hf)
        return self.manual_hf()

    def report(self, which: str) -> dict:
        d = self.paths.report_dir(which)
        md = d / "eda_summary.md"
        figs = sorted((d / "figures").glob("*.png")) if (d / "figures").exists() else []
        return {
            "exists": md.exists(),
            "markdown": md.read_text(encoding="utf-8") if md.exists() else "",
            "mtime": md.stat().st_mtime if md.exists() else None,
            "figures": [{"name": f.name, "url": f"/reports/{which}/figures/{f.name}?v={int(f.stat().st_mtime)}"} for f in figs],
        }

    def run_sql(self, which: str, query: str) -> dict:
        conn = self._connect_ro(which)
        if conn is None:
            raise FileNotFoundError("There is no database for this dataset yet.")
        conn.row_factory = None

        def authorizer(action, *_):
            # 21 SELECT, 20 READ, 31 FUNCTION, 33 RECURSIVE
            return sqlite3.SQLITE_OK if action in (21, 20, 31, 33) else sqlite3.SQLITE_DENY

        deadline = time.time() + SQL_TIME_LIMIT_S
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10_000)
        started = time.time()
        try:
            cur = conn.execute(query)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(SQL_ROW_LIMIT + 1)
        except sqlite3.Error as exc:
            raise ValueError(f"{exc}") from None
        finally:
            conn.close()
        truncated = len(rows) > SQL_ROW_LIMIT
        rows = [[v if not isinstance(v, bytes) else v.hex() for v in r] for r in rows[:SQL_ROW_LIMIT]]
        return {"columns": cols, "rows": rows, "truncated": truncated, "ms": round((time.time() - started) * 1000)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AIModelsDashboard/0.2"
    app: App  # set by make_server

    def log_message(self, fmt: str, *args) -> None:  # silence the per-request noise
        log.debug(fmt, *args)

    # ---- plumbing --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, allow_nan=False, default=str).encode(), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name.lower() in ("127.0.0.1", "localhost", "[::1]")

    def _authorised(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), self.app.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    # ---- GET -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "bad host")
        url = urlparse(self.path)
        path, qs = unquote(url.path), parse_qs(url.query)
        which = "live" if qs.get("db", ["sample"])[0] == "live" else "sample"
        try:
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", self.app.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name not in ALLOWED_STATIC:
                    return self._error(404, "not found")
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return self._send(200, (WEB_DIR / name).read_bytes(), f"{ctype}; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/status":
                return self._json(self.app.status())
            if path == "/api/models":
                data = self.app.models(which)
                return self._json(data) if data else self._error(404, "no data yet")
            if path == "/api/quality":
                data = self.app.quality(which)
                return self._json(data) if data else self._error(404, "no data yet")
            if path == "/api/report":
                return self._json(self.app.report(which))
            if path.startswith("/api/jobs/"):
                job = self.app.jobs.get(path.rsplit("/", 1)[-1])
                if not job:
                    return self._error(404, "unknown job")
                return self._json(job.to_dict(since=int(qs.get("since", ["0"])[0] or 0)))
            if path.startswith("/reports/"):
                return self._serve_report(path)
        except (ValueError, FileNotFoundError) as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s failed", path)
            return self._error(500, f"{exc.__class__.__name__}: {exc}")
        return self._error(404, "not found")

    def _serve_report(self, path: str) -> None:
        parts = path.split("/")  # ['', 'reports', which, ...]
        if len(parts) < 4 or parts[2] not in ("sample", "live"):
            return self._error(404, "not found")
        base = self.app.paths.report_dir(parts[2]).resolve()
        target = (base / "/".join(parts[3:])).resolve()
        if base not in target.parents or target.suffix not in (".png", ".md") or not target.is_file():
            return self._error(404, "not found")
        self._send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    # ---- POST ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "bad host")
        if not self._authorised():
            return self._error(403, "missing or wrong dashboard token - reload the page")
        path = urlparse(self.path).path
        try:
            body = self._body()
            which = "live" if body.get("dataset") == "live" else "sample"
            if path == "/api/run":
                job = self.app.start_job(str(body.get("kind", "all")), body)
                return self._json(job.to_dict(), 202)
            if path == "/api/keys":
                for field_name, env_name in (("aa_api_key", "AA_API_KEY"), ("hf_token", "HF_TOKEN")):
                    if field_name in body:
                        value = str(body[field_name] or "").strip()
                        if value:
                            self.app.keys[env_name] = value
                        else:
                            self.app.keys.pop(env_name, None)
                            os.environ.pop(env_name, None)
                return self._json({"aa": bool(self.app.keys.get("AA_API_KEY") or os.environ.get("AA_API_KEY")),
                                   "hf": bool(self.app.keys.get("HF_TOKEN") or os.environ.get("HF_TOKEN"))})
            if path == "/api/diagnose":
                return self._json({"results": self.app.diagnose()})
            if path == "/api/sql":
                return self._json(self.app.run_sql(which, str(body.get("query", ""))))
            if path == "/api/manual_hf":
                return self._json({"manual_hf": self.app.edit_manual_hf(
                    str(body.get("op", "")), str(body.get("match_key", "")), str(body.get("hf_repo_id", "")))})
        except RuntimeError as exc:
            return self._error(409, str(exc))
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s failed", path)
            return self._error(500, f"{exc.__class__.__name__}: {exc}")
        return self._error(404, "not found")


def make_server(app: App, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(port: int = 8765, open_browser: bool = True, paths: Paths | None = None) -> None:
    app = App(paths)
    server = None
    for candidate in range(port, port + 20):  # first free port wins
        try:
            server = make_server(app, candidate)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"Could not find a free port in {port}-{port + 19}.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\n  AI Model Dashboard running at {url}\n  Press Ctrl+C (or close this window) to stop.\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        server.server_close()


__all__ = ["App", "Paths", "make_server", "serve"]
