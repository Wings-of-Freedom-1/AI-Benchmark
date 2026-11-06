import sys
import os
import json
import requests
from urllib.parse import parse_qs
from pathlib import Path

# 1. PATH SETUP
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))
from aimodels.dashboard import App, Paths

# 2. VERCEL BLOB CONFIG
TMP_DB_PATH = "/tmp/models.db"
BLOB_FILENAME = "models.db"
TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://vercel-storage.com"

def download_db():
    if os.path.exists(TMP_DB_PATH): return
    try:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        # List blobs matching the filename prefix
        r = requests.get(f"{BLOB_API}?prefix={BLOB_FILENAME}", headers=headers)
        blobs = r.json().get("blobs", [])
        if blobs:
            # Download the file content using its unique public URL
            blob_url = blobs[0]["url"]
            with open(TMP_DB_PATH, "wb") as f:
                f.write(requests.get(blob_url).content)
    except Exception as e:
        print(f"Download error: {e}")

def upload_db():
    if not os.path.exists(TMP_DB_PATH): return
    try:
        with open(TMP_DB_PATH, "rb") as f:
            binary_data = f.read()

        # Vercel Blob API expects raw body data instead of multipart/form-data
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "x-api-version": "1",
            "X-Blob-Store-Timeout": "30"
        }
        
        # Target the specific destination path on Vercel Blob
        upload_url = f"{BLOB_API}/{BLOB_FILENAME}"
        
        r = requests.put(
            upload_url,
            headers=headers,
            data=binary_data,
            timeout=30
        )
        if r.status_code not in:
            print(f"Upload failed status code: {r.status_code}, response: {r.text}")
    except Exception as e:
        print(f"Upload error: {e}")

# 3. APP SETUP (Pointing to /tmp to prevent Read-only errors)
paths = Paths()
paths.live_db = Path(TMP_DB_PATH)
paths.sample_db = Path(TMP_DB_PATH)
paths.raw = Path("/tmp/raw")

# Ensure /tmp/raw exists so the ETL pipeline doesn't throw a FileNotFoundError
paths.raw.mkdir(parents=True, exist_ok=True)

app_instance = App(paths)

# 4. ROUTER
def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    
    download_db()

    if path.startswith("/api/"):
        if method == "GET":
            if path == "/api/status": data = app_instance.status()
            elif path == "/api/models": data = app_instance.models("live")
            elif path == "/api/quality": data = app_instance.quality("live")
            elif path == "/api/report": data = app_instance.report("live")
            else:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"API Not Found"]

            if data is None:
                start_response("404 Not Found", [("Content-Type", "application/json")])
                return [b'{"error": "no data yet"}']

            res = json.dumps(data, default=str).encode("utf-8")
            start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(res)))])
            return [res]

        elif method == "POST":
            if path == "/api/run":
                try:
                    from aimodels.pipeline import run_etl
                    
                    # Ensure directories exist right before running the pipeline 
                    Path("/tmp/raw").mkdir(parents=True, exist_ok=True)
                    
                    summary = run_etl(
                        offline=False, use_cache=False, skip_hf=False, 
                        db_path=Path(TMP_DB_PATH),
                        raw_dir=Path("/tmp/raw")
                    )
                    upload_db()
                    
                    res = json.dumps({"status": "done", "n_valid": summary.n_valid}).encode("utf-8")
                    start_response("200 OK", [("Content-Type", "application/json")])
                    return [res]
                except Exception as e:
                    start_response("500 Internal Error", [("Content-Type", "application/json")])
                    return [json.dumps({"error": str(e)}).encode("utf-8")]

            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]

    # SERVE FRONTEND
    web_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "aimodels", "web"))
    if path == "/" or path == "/index.html":
        filepath = os.path.join(web_dir, "index.html")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", app_instance.token)
            res = html.encode("utf-8")
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(res)))])
            return [res]

    if path.startswith("/static/"):
        filename = path.replace("/static/", "")
        filepath = os.path.join(web_dir, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, "rb") as f: content = f.read()
            ctype = "text/css" if filename.endswith(".css") else "application/javascript" if filename.endswith(".js") else "application/octet-stream"
            start_response("200 OK", [("Content-Type", ctype), ("Content-Length", str(len(content)))])
            return [content]

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"Not Found"]

# 5. VERCEL ENTRYPOINT EXPORT
# This assignment makes the application visible to Vercel's runtime routing engine.
handler = app
