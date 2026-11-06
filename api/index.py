from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Paths and Vercel Blob settings
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
WEB_DIR = SRC_DIR / "aimodels" / "web"
BUNDLED_DATA_DIR = ROOT / "data"
BUNDLED_REPORTS_DIR = ROOT / "reports"

TMP_ROOT = Path("/tmp/ai-benchmark")
DATA_DIR = TMP_ROOT / "data"
LIVE_DB = DATA_DIR / "models.db"
SAMPLE_DB = DATA_DIR / "sample.db"
RAW_DIR = DATA_DIR / "raw"
MANUAL_HF_FILE = DATA_DIR / "manual_hf_ids.csv"
REPORTS_DIR = TMP_ROOT / "reports"
STATE_FILE = TMP_ROOT / "state.json"

BLOB_API = "https://blob.vercel-storage.com"
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
BLOB_ACCESS = os.environ.get("BLOB_ACCESS", "public").strip().lower()
BLOB_API_VERSION = os.environ.get("BLOB_API_VERSION", "7").strip()
STATE_PREFIX = "ai-benchmark-state/"
MAX_JOBS = 20
MAX_BODY_BYTES = 1_000_000
COOKIE_NAME = "aimodels_session_keys"
COOKIE_MAX_AGE = 7 * 24 * 60 * 60
ALLOWED_STATIC = {"app.css", "app.js", "charts.js"}

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Capture deployment-level keys once. Session keys are layered on top for a
# single request and never written to the Blob snapshot.
DEPLOYMENT_KEYS = {
    "AA_API_KEY": os.environ.get("AA_API_KEY"),
    "HF_TOKEN": os.environ.get("HF_TOKEN"),
}

_REQUEST_LOCK = threading.RLock()
_STATE_META: dict = {"jobs": {}, "latest_job_id": None}
_STATE_READY = False
_STATE_LOADED_AT = 0.0
STATE_CACHE_SECONDS = 15


# ---------------------------------------------------------------------------
# Helpers: responses, request bodies, and encrypted per-browser API-key cookie
# ---------------------------------------------------------------------------


def respond(start_response, status: str, body, content_type: str = "application/json",
            extra_headers: list[tuple[str, str]] | None = None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False, default=str, allow_nan=False)
    if isinstance(body, str):
        body = body.encode("utf-8")

    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store, max-age=0"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def read_json_body(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        raise ValueError("Invalid Content-Length header") from None

    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError("Request body is too large")
    raw = environ.get("wsgi.input").read(length) if length else b"{}"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Request body must be valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Request JSON must be an object")
    return value


def _session_encryption_key() -> bytes:
    secret = os.environ.get("DASHBOARD_SESSION_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "DASHBOARD_SESSION_SECRET is missing. Add a long random value in Vercel Environment Variables."
        )
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _get_cookie_value(environ, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(environ.get("HTTP_COOKIE", ""))
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def get_session_keys(environ) -> dict[str, str]:
    """Decrypt API keys from this browser's HttpOnly cookie, if present."""
    value = _get_cookie_value(environ, COOKIE_NAME)
    if not value:
        return {}
    try:
        padded = value + ("=" * (-len(value) % 4))
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        nonce, ciphertext = payload[:12], payload[12:]
        plaintext = AESGCM(_session_encryption_key()).decrypt(
            nonce, ciphertext, COOKIE_NAME.encode("utf-8")
        )
        parsed = json.loads(plaintext.decode("utf-8"))
        if not isinstance(parsed, dict):
            return {}
        return {
            key: str(parsed.get(key, ""))
            for key in ("AA_API_KEY", "HF_TOKEN")
            if parsed.get(key)
        }
    except Exception:
        # An expired or undecryptable cookie should not break the dashboard.
        return {}


def make_session_cookie(keys: dict[str, str]) -> str:
    if not keys:
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"

    nonce = os.urandom(12)
    plaintext = json.dumps(keys, separators=(",", ":")).encode("utf-8")
    encrypted = AESGCM(_session_encryption_key()).encrypt(
        nonce, plaintext, COOKIE_NAME.encode("utf-8")
    )
    value = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")
    return (
        f"{COOKIE_NAME}={value}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def apply_request_keys(app_instance, environ) -> dict[str, str]:
    """Restore the deployment keys, then layer this visitor's session keys."""
    for name, value in DEPLOYMENT_KEYS.items():
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    keys = get_session_keys(environ)
    app_instance.keys = dict(keys)
    for name, value in keys.items():
        os.environ[name] = value
    return keys


# ---------------------------------------------------------------------------
# Vercel Blob REST operations
# ---------------------------------------------------------------------------


def blob_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {BLOB_TOKEN}",
        "x-api-version": BLOB_API_VERSION,
        "x-vercel-blob-access": BLOB_ACCESS,
    }
    if extra:
        headers.update(extra)
    return headers


def list_state_blobs() -> list[dict]:
    if not BLOB_TOKEN:
        return []
    if BLOB_ACCESS not in ("public", "private"):
        raise RuntimeError("BLOB_ACCESS must be either 'public' or 'private'.")

    response = requests.get(
        BLOB_API,
        params={"prefix": STATE_PREFIX, "limit": 1000},
        headers=blob_headers(),
        timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(
            f"Vercel Blob list failed ({response.status_code}): {response.text[:500]}"
        )
    blobs = response.json().get("blobs", [])
    return [b for b in blobs if str(b.get("pathname", "")).startswith(STATE_PREFIX)]


def download_latest_state() -> bool:
    """Download and safely extract the newest immutable state snapshot."""
    blobs = list_state_blobs()
    if not blobs:
        return False

    # Snapshot pathnames begin with a UTC timestamp, so lexical ordering is
    # deterministic even if several uploads happen within the same second.
    blobs.sort(key=lambda item: str(item.get("pathname", "")), reverse=True)
    latest = blobs[0]
    download_url = latest.get("downloadUrl") or latest.get("url")
    if not download_url:
        raise RuntimeError("The latest Vercel Blob snapshot has no download URL.")

    response = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {BLOB_TOKEN}"},
        timeout=(10, 120),
    )
    if not response.ok:
        raise RuntimeError(
            f"Vercel Blob download failed ({response.status_code}): {response.text[:500]}"
        )

    archive_path = TMP_ROOT.with_suffix(".zip")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(response.content)

    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    root_resolved = TMP_ROOT.resolve()
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError("The Vercel Blob snapshot contains an unsafe path.")
                target = (TMP_ROOT / member_path).resolve()
                if target != root_resolved and root_resolved not in target.parents:
                    raise RuntimeError("The Vercel Blob snapshot contains an unsafe path.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
    except zipfile.BadZipFile:
        raise RuntimeError("The latest Vercel Blob state snapshot is not a valid ZIP file.") from None
    finally:
        archive_path.unlink(missing_ok=True)

    return True


def download_legacy_live_database() -> bool:
    """Import the old single-file models.db Blob used by the previous adapter."""
    if not BLOB_TOKEN:
        return False
    response = requests.get(
        BLOB_API,
        params={"prefix": "models.db", "limit": 10},
        headers=blob_headers(),
        timeout=(10, 30),
    )
    if not response.ok:
        raise RuntimeError(
            f"Vercel Blob legacy database lookup failed ({response.status_code}): {response.text[:500]}"
        )
    matches = [
        item for item in response.json().get("blobs", [])
        if item.get("pathname") == "models.db"
    ]
    if not matches:
        return False
    blob = matches[0]
    url = blob.get("downloadUrl") or blob.get("url")
    if not url:
        raise RuntimeError("The legacy models.db Blob has no download URL.")
    downloaded = requests.get(
        url,
        headers={"Authorization": f"Bearer {BLOB_TOKEN}"},
        timeout=(10, 120),
    )
    if not downloaded.ok:
        raise RuntimeError(
            f"Legacy models.db download failed ({downloaded.status_code}): {downloaded.text[:500]}"
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = LIVE_DB.with_suffix(".legacy-download")
    temporary.write_bytes(downloaded.content)
    os.replace(temporary, LIVE_DB)
    return True


def copy_bundled_state() -> None:
    """Use repository data as the first-deploy fallback before Blob has a snapshot."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for filename in ("models.db", "sample.db", "manual_hf_ids.csv"):
        source = BUNDLED_DATA_DIR / filename
        target = DATA_DIR / filename
        if source.is_file() and not target.exists():
            shutil.copy2(source, target)

    bundled_raw = BUNDLED_DATA_DIR / "raw"
    if bundled_raw.is_dir():
        shutil.copytree(bundled_raw, RAW_DIR, dirs_exist_ok=True)

    if BUNDLED_REPORTS_DIR.is_dir():
        shutil.copytree(BUNDLED_REPORTS_DIR, REPORTS_DIR, dirs_exist_ok=True)


def load_state(force: bool = False) -> None:
    """Load the latest snapshot; reuse a short-lived per-instance cache for reads."""
    global _STATE_META, _STATE_READY, _STATE_LOADED_AT
    if (
        not force
        and _STATE_READY
        and (time.monotonic() - _STATE_LOADED_AT) < STATE_CACHE_SECONDS
    ):
        return

    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    loaded = download_latest_state() if BLOB_TOKEN else False
    if not loaded:
        copy_bundled_state()
        # Older deployments stored only data/models.db at the Blob root.
        # Import it once when no versioned state archive exists.
        if BLOB_TOKEN:
            download_legacy_live_database()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    _STATE_META = {"jobs": {}, "latest_job_id": None}
    if STATE_FILE.is_file():
        try:
            parsed = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                _STATE_META.update(parsed)
        except (OSError, json.JSONDecodeError):
            _STATE_META = {"jobs": {}, "latest_job_id": None}
    if not isinstance(_STATE_META.get("jobs"), dict):
        _STATE_META["jobs"] = {}
    _STATE_READY = True
    _STATE_LOADED_AT = time.monotonic()


def save_state() -> str:
    """Persist databases, raw cache, manual HF mapping, reports and job logs."""
    if not BLOB_TOKEN:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set; the run cannot be persisted.")

    STATE_FILE.write_text(
        json.dumps(_STATE_META, ensure_ascii=False, default=str), encoding="utf-8"
    )
    archive_path = TMP_ROOT.with_suffix(".upload.zip")
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for folder in (DATA_DIR, REPORTS_DIR):
            if not folder.exists():
                continue
            for file_path in folder.rglob("*"):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(TMP_ROOT).as_posix())
        archive.write(STATE_FILE, "state.json")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    pathname = f"{STATE_PREFIX}{stamp}-{uuid.uuid4().hex}.zip"
    try:
        with archive_path.open("rb") as stream:
            response = requests.put(
                BLOB_API,
                params={"pathname": pathname},
                headers=blob_headers({
                    "x-add-random-suffix": "0",
                    "x-content-type": "application/zip",
                    "Content-Type": "application/zip",
                    "x-cache-control-max-age": "60",
                }),
                data=stream,
                timeout=(10, 180),
            )
        if not response.ok:
            raise RuntimeError(
                f"Vercel Blob upload failed ({response.status_code}): {response.text[:700]}"
            )
    finally:
        archive_path.unlink(missing_ok=True)

    global _STATE_READY, _STATE_LOADED_AT
    _STATE_READY = True
    _STATE_LOADED_AT = time.monotonic()
    return pathname


# ---------------------------------------------------------------------------
# Application construction and route helpers
# ---------------------------------------------------------------------------


def make_app(environ):
    # config.py derives DATA_DIR, RAW_DIR and cache paths from AIMODELS_HOME.
    # Set it before importing the dashboard/pipeline modules.
    os.environ["AIMODELS_HOME"] = str(TMP_ROOT)
    os.environ["AIMODELS_DB"] = str(LIVE_DB)

    from aimodels.dashboard import App, Paths

    paths = Paths(
        live_db=LIVE_DB,
        sample_db=SAMPLE_DB,
        reports=REPORTS_DIR,
        manual_hf=MANUAL_HF_FILE,
        raw=RAW_DIR,
    )
    app_instance = App(paths)
    apply_request_keys(app_instance, environ)
    return app_instance


def selected_dataset(query: dict, body: dict | None = None) -> str:
    value = None
    if body is not None:
        value = body.get("dataset")
    if value is None:
        value = query.get("db", ["sample"])[0]
    return "live" if value == "live" else "sample"


def get_job(job_id: str, since: int = 0) -> dict | None:
    stored = _STATE_META.get("jobs", {}).get(job_id)
    if not isinstance(stored, dict):
        return None
    result = dict(stored)
    lines = result.get("lines", [])
    if not isinstance(lines, list):
        lines = []
    since = max(0, since)
    result["lines"] = lines[since:]
    result["next"] = len(lines)
    return result


def serve_report(app_instance, path: str, start_response):
    # /reports/<sample|live>/<relative report path>
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "reports" or parts[2] not in ("sample", "live"):
        return respond(start_response, "404 Not Found", {"error": "not found"})

    base = app_instance.paths.report_dir(parts[2]).resolve()
    target = (base / "/".join(parts[3:])).resolve()
    if (
        base not in target.parents
        or target.suffix.lower() not in (".png", ".md")
        or not target.is_file()
    ):
        return respond(start_response, "404 Not Found", {"error": "not found"})

    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return respond(start_response, "200 OK", target.read_bytes(), content_type)


def handle_get(path: str, query: dict, environ, start_response):
    if path in ("/", "/index.html"):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__TOKEN__", secrets.token_urlsafe(24))
        return respond(start_response, "200 OK", html, "text/html; charset=utf-8")

    if path == "/favicon.ico":
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    if path.startswith("/static/"):
        name = path[len("/static/"):]
        if name not in ALLOWED_STATIC:
            return respond(start_response, "404 Not Found", {"error": "not found"})
        file_path = WEB_DIR / name
        if not file_path.is_file():
            return respond(start_response, "404 Not Found", {"error": "not found"})
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return respond(start_response, "200 OK", file_path.read_bytes(), content_type)

    if not (path.startswith("/api/") or path.startswith("/reports/")):
        return respond(start_response, "404 Not Found", {"error": "not found"}, "text/plain")

    load_state()
    app_instance = make_app(environ)

    if path == "/api/status":
        status = app_instance.status()
        latest_id = _STATE_META.get("latest_job_id")
        latest = get_job(str(latest_id), since=10**9) if latest_id else None
        status["job"] = latest
        return respond(start_response, "200 OK", status)

    if path == "/api/models":
        data = app_instance.models(selected_dataset(query))
        if data is None:
            return respond(start_response, "404 Not Found", {"error": "no data yet"})
        return respond(start_response, "200 OK", data)

    if path == "/api/quality":
        data = app_instance.quality(selected_dataset(query))
        if data is None:
            return respond(start_response, "404 Not Found", {"error": "no data yet"})
        return respond(start_response, "200 OK", data)

    if path == "/api/report":
        return respond(start_response, "200 OK", app_instance.report(selected_dataset(query)))

    if path.startswith("/api/jobs/"):
        job_id = path.rsplit("/", 1)[-1]
        try:
            since = int(query.get("since", ["0"])[0] or 0)
        except (TypeError, ValueError):
            since = 0
        job = get_job(job_id, since)
        if job is None:
            return respond(start_response, "404 Not Found", {"error": "unknown job"})
        return respond(start_response, "200 OK", job)

    if path.startswith("/reports/"):
        return serve_report(app_instance, path, start_response)

    if path == "/api/health":
        return respond(start_response, "200 OK", {"ok": True, "blob_configured": bool(BLOB_TOKEN)})

    return respond(start_response, "404 Not Found", {"error": "not found"})


def handle_post(path: str, body: dict, environ, start_response):
    if path == "/api/keys":
        keys = get_session_keys(environ)
        for field, env_name in (("aa_api_key", "AA_API_KEY"), ("hf_token", "HF_TOKEN")):
            if field in body:
                value = str(body.get(field) or "").strip()
                if value:
                    keys[env_name] = value
                else:
                    keys.pop(env_name, None)

        cookie = make_session_cookie(keys)
        return respond(
            start_response,
            "200 OK",
            {
                "aa": bool(keys.get("AA_API_KEY") or DEPLOYMENT_KEYS.get("AA_API_KEY")),
                "hf": bool(keys.get("HF_TOKEN") or DEPLOYMENT_KEYS.get("HF_TOKEN")),
            },
            extra_headers=[("Set-Cookie", cookie)],
        )

    # All remaining POST routes need the newest durable data snapshot.
    load_state(force=True)
    app_instance = make_app(environ)

    if path == "/api/run":
        if not BLOB_TOKEN:
            return respond(
                start_response,
                "503 Service Unavailable",
                {"error": "Vercel Blob is not configured. Set BLOB_READ_WRITE_TOKEN before running the pipeline."},
            )

        from aimodels.dashboard import Job

        kind = str(body.get("kind", "all"))
        if kind not in ("etl", "eda", "all"):
            return respond(start_response, "400 Bad Request", {"error": "kind must be etl, eda or all"})

        params = app_instance._clean_params(body)
        job = Job(kind, params)
        app_instance.current = job
        job.log("INFO", f"Starting {kind.upper()} on {params['dataset']} data.")

        # Run synchronously in this invocation. App._run_job is the same method
        # used by localhost and preserves the existing ETL/EDA implementation,
        # stage updates, validation behavior and Python log capture.
        app_instance._run_job(job)

        job.result["saved_to_blob"] = True
        _STATE_META.setdefault("jobs", {})[job.id] = job.to_dict()
        _STATE_META["latest_job_id"] = job.id

        # Keep recent jobs/logs without allowing the state archive to grow forever.
        jobs = _STATE_META["jobs"]
        ordered = sorted(
            jobs.items(),
            key=lambda item: float(item[1].get("started") or 0),
            reverse=True,
        )
        _STATE_META["jobs"] = dict(ordered[:MAX_JOBS])

        try:
            save_state()
        except Exception as exc:
            traceback.print_exc()
            return respond(
                start_response,
                "502 Bad Gateway",
                {
                    "error": (
                        "The pipeline finished, but saving its database/reports to Vercel Blob failed. "
                        f"{exc}"
                    )
                },
            )

        return respond(start_response, "202 Accepted", job.to_dict())

    if path == "/api/diagnose":
        return respond(start_response, "200 OK", {"results": app_instance.diagnose()})

    if path == "/api/sql":
        which = selected_dataset({}, body)
        result = app_instance.run_sql(which, str(body.get("query", "")))
        return respond(start_response, "200 OK", result)

    if path == "/api/manual_hf":
        result = app_instance.edit_manual_hf(
            str(body.get("op", "")),
            str(body.get("match_key", "")),
            str(body.get("hf_repo_id", "")),
        )
        try:
            save_state()
        except Exception as exc:
            return respond(
                start_response,
                "502 Bad Gateway",
                {"error": f"The mapping changed in this invocation but could not be saved to Blob: {exc}"},
            )
        return respond(start_response, "200 OK", {"manual_hf": result})

    return respond(start_response, "404 Not Found", {"error": "not found"})


# ---------------------------------------------------------------------------
# WSGI entry point used by Vercel's rewrite in vercel.json
# ---------------------------------------------------------------------------


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)

    # vercel.json rewrites every route to /api/index?p=/<original path>.
    if "p" in query and query["p"]:
        path = query["p"][0]
    elif path in ("/api/index", "/api/index.py"):
        path = "/"
    if not path.startswith("/"):
        path = "/" + path

    method = environ.get("REQUEST_METHOD", "GET").upper()

    # Vercel can reuse one Python process for overlapping invocations. Protect
    # its shared /tmp workspace from concurrent mutation within that process.
    with _REQUEST_LOCK:
        try:
            if method == "GET":
                return handle_get(path, query, environ, start_response)
            if method == "POST":
                body = read_json_body(environ)
                return handle_post(path, body, environ, start_response)
            return respond(
                start_response,
                "405 Method Not Allowed",
                {"error": "method not allowed"},
                extra_headers=[("Allow", "GET, POST")],
            )
        except (ValueError, FileNotFoundError) as exc:
            return respond(start_response, "400 Bad Request", {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface deployment failures in Vercel logs
            traceback.print_exc()
            return respond(
                start_response,
                "500 Internal Server Error",
                {"error": f"{exc.__class__.__name__}: {exc}"},
            )


# Vercel's Python runtime discovers the WSGI application using this name.
app = application
