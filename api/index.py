import sys, os, json, shutil, time, traceback
from pathlib import Path
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

ROOT = Path(__file__).resolve().parent.parent
TMP_DB = Path("/tmp/models.db")
TMP_RAW = Path("/tmp/raw")
BUNDLED_DB = ROOT / "data" / "models.db"
BLOB_NAME = "models.db"
BLOB_API = "https://blob.vercel-storage.com"
TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
REFRESH_SECONDS = 60

TMP_RAW.mkdir(parents=True, exist_ok=True)


def blob_headers(extra=None):
    h = {"Authorization": f"Bearer {TOKEN}", "x-api-version": "7"}
    if extra:
        h.update(extra)
    return h


def download_from_blob():
    """Pull latest DB from Blob into /tmp. Returns True on success."""
    if not TOKEN:
        return False
    try:
        r = requests.get(BLOB_API, params={"prefix": BLOB_NAME, "limit": 5},
                         headers=blob_headers(), timeout=10)
        r.raise_for_status()
        blobs = [b for b in r.json().get("blobs", []) if b.get("pathname") == BLOB_NAME]
        if not blobs:
            return False
        url = blobs[0].get("downloadUrl") or blobs[0]["url"]
        d = requests.get(url, timeout=20)
        d.raise_for_status()
        tmp = TMP_DB.with_suffix(".dl")
        tmp.write_bytes(d.content)
        os.replace(tmp, TMP_DB)
        return True
    except Exception as e:
        print(f"Blob download failed: {e}", file=sys.stderr)
        return False


def upload_to_blob():
    if not TOKEN:
        return
    with open(TMP_DB, "rb") as f:
        r = requests.put(
            f"{BLOB_API}/{BLOB_NAME}",
            headers=blob_headers({
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1",
                "x-content-type": "application/x-sqlite3",
                "x-cache-control-max-age": "60",
            }),
            data=f, timeout=60,
        )
    r.raise_for_status()


def ensure_db(force=False):
    fresh = TMP_DB.exists() and (time.time() - TMP_DB.stat().st_mtime) < REFRESH_SECONDS
    if fresh and not force:
        return
    if download_from_blob():
        return
    if not TMP_DB.exists() and BUNDLED_DB.exists():
        shutil.copy(BUNDLED_DB, TMP_DB)


# Get the DB BEFORE the App is built
ensure_db(force=True)

from aimodels.dashboard import App, Paths  # noqa: E402

paths = Paths()
paths.live_db = TMP_DB
paths.sample_db = TMP_DB
if hasattr(paths, "raw"):
    paths.raw = TMP_RAW
app_instance = App(paths)


def respond(start_response, status, body, ctype="application/json"):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, default=str)
    if isinstance(body, str):
        body = body.encode("utf-8")
    start_response(status, [("Content-Type", ctype), ("Content-Length", str(len(body)))])
    return [body]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")

    try:
        if path.startswith("/api/"):
            ensure_db()
            if method == "GET":
                routes = {
                    "/api/status": lambda: app_instance.status(),
                    "/api/models": lambda: app_instance.models("live"),
                    "/api/quality": lambda: app_instance.quality("live"),
                    "/api/report": lambda: app_instance.report("live"),
                }
                if path not in routes:
                    return respond(start_response, "404 Not Found", {"error": "not found"})
                data = routes[path]()
                if data is
