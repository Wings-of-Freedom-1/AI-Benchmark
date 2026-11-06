"""Vercel adapter for the AI Benchmark dashboard.

The local dashboard uses an in-memory background-thread job registry. That model
cannot be shared reliably between independent Vercel Function invocations.
This adapter therefore runs each job inside POST /api/run, captures the same
Job object/log format used locally, and stores the resulting state snapshot in
Vercel Blob. The existing frontend can keep polling /api/jobs/{id} unchanged.
"""

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
from urllib.parse import parse_qs, quote

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
