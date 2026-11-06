import sys, os, json, shutil, time, traceback
from urllib.parse import parse_qs
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


def application(environ, start_response):
    # The rewrite in vercel.json passes the real path in ?p=
    path = environ.get("PATH_INFO", "/")
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    if "p" in qs:
        path = qs["p"][0]
    elif path in ("/api/index", "/api/index.py"):
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
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
                if data is None:
                    return respond(start_response, "404 Not Found",
                                   {"error": "no data yet - run ETL"})
                return respond(start_response, "200 OK", data)

            if method == "POST" and path == "/api/run":
                from aimodels.pipeline import run_etl
                summary = run_etl(offline=False, use_cache=False, skip_hf=False,
                                  db_path=TMP_DB, raw_dir=TMP_RAW)
                upload_to_blob()
                return respond(start_response, "200 OK",
                               {"status": "done", "n_valid": getattr(summary, "n_valid", None)})

            return respond(start_response, "404 Not Found", {"error": "not found"})

        # ---- Frontend ----
        web_dir = ROOT / "src" / "aimodels" / "web"

        if path in ("/", "/index.html"):
            html = (web_dir / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", str(getattr(app_instance, "token", "")))
            return respond(start_response, "200 OK", html, "text/html; charset=utf-8")

        if path.startswith("/static/"):
            fp = (web_dir / path[len("/static/"):]).resolve()
            if web_dir.resolve() in fp.parents and fp.is_file():
                ctype = {".css": "text/css", ".js": "application/javascript"}.get(
                    fp.suffix, "application/octet-stream")
                return respond(start_response, "200 OK", fp.read_bytes(), ctype)

        return respond(start_response, "404 Not Found", "Not Found", "text/plain")

    except Exception as e:
        traceback.print_exc()
        return respond(start_response, "500 Internal Server Error", {"error": str(e)})


app = application
