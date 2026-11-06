import sys
import os
import json
import requests
from urllib.parse import parse_qs
from pathlib import Path

# Add the 'src' directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from aimodels.dashboard import App, Paths

# --- BLOB STORAGE CONFIG ---
TMP_DB_PATH = "/tmp/models.db"
BLOB_FILENAME = "models.db"
TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
BLOB_API = "https://blob.vercel-storage.com"

def sync_db_from_cloud():
    """Download the latest database from Vercel Blob to /tmp"""
    if os.path.exists(TMP_DB_PATH):
        return
    try:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r = requests.get(f"{BLOB_API}?prefix={BLOB_FILENAME}", headers=headers, timeout=10)
        r.raise_for_status()
        blobs = r.json().get("blobs", [])
        if not blobs:
            print("No database found in Blob storage. Starting fresh.")
            return
        
        download_url = blobs[0]["url"]
        r = requests.get(download_url, headers=headers, timeout=10)
        r.raise_for_status()
        with open(TMP_DB_PATH, "wb") as f:
            f.write(r.content)
        print("Successfully downloaded DB from Blob.")
    except Exception as e:
        print(f"Error downloading DB: {e}")

def sync_db_to_cloud():
    """Upload the updated database from /tmp back to Vercel Blob"""
    try:
        with open(TMP_DB_PATH, "rb") as f:
            files = {"file": (BLOB_FILENAME, f, "application/x-sqlite3")}
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "x-api-version": "1",
            }
            # You can change access to "private" if you prefer, but "public" makes serving easier.
            r = requests.post(
                BLOB_API,
                headers=headers,
                files=files,
                data={"access": "public"}, 
                timeout=30,
            )
            r.raise_for_status()
            print("Successfully uploaded DB to Blob.")
    except Exception as e:
        print(f"Error uploading DB: {e}")

# --- APP SETUP ---
# Create App instance and point ALL file writing to /tmp
paths = Paths()
paths.live_db = Path(TMP_DB_PATH)
paths.sample_db = Path(TMP_DB_PATH)
paths.raw = Path("/tmp/raw")             # Prevents raw JSON write error
paths.reports = Path("/tmp/reports")     # Prevents chart generation write error

app_instance = App(paths)

# --- WSGI ROUTER ---
def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    
    # Ensure we have the latest DB downloaded before doing anything
    sync_db_from_cloud()

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
                return [b'{"error": "no data yet - run ETL"}']

            res = json.dumps(data, default=str).encode("utf-8")
            start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(res)))])
            return [res]

        elif method == "POST":
            if path == "/api/run":
                try:
                    from aimodels.pipeline import run_etl
                    
                    # Pass the /tmp/raw directory to the pipeline to fix the Read-only error!
                    summary = run_etl(
                        offline=False, 
                        use_cache=False, 
                        skip_hf=False, 
                        db_path=Path(TMP_DB_PATH),
                        raw_dir=Path("/tmp/raw") 
                    )
                    
                    # Upload the new database to the cloud
                    sync_db_to_cloud()
                    
                    res = json.dumps({"status": "done", "n_valid": summary.n_valid}).encode("utf-8")
                    start_response("200 OK", [("Content-Type", "application/json")])
                    return [res]
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    start_response("500 Internal Error", [("Content-Type", "application/json")])
                    return [json.dumps({"error": str(e)}).encode("utf-8")]

            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not Found"]

    # --- SERVE FRONTEND ---
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
