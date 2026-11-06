import sys
import os
import json
import requests
from urllib.parse import parse_qs

# Add the 'src' directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from aimodels.dashboard import App, Paths

TMP_DB_PATH = "/tmp/models.db"
BLOB_FILENAME = "models.db"
TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN")
# The Vercel Blob REST endpoint
BLOB_API = "https://blob.vercel-storage.com"

def sync_db_from_cloud():
    """Download the latest database from Vercel Blob to /tmp using plain HTTP."""
    if os.path.exists(TMP_DB_PATH):
        return
    try:
        # List blobs with the given prefix
        headers = {"Authorization": f"Bearer {TOKEN}"}
        r = requests.get(f"{BLOB_API}?prefix={BLOB_FILENAME}", headers=headers, timeout=10)
        r.raise_for_status()
        blobs = r.json().get("blobs", [])
        if not blobs:
            print("No database found in Blob storage.")
            return
        
        # Download the first matching blob
        download_url = blobs[0]["url"]
        # For private blobs, the download URL itself may also require the token
        r = requests.get(download_url, headers=headers, timeout=10)
        r.raise_for_status()
        with open(TMP_DB_PATH, "wb") as f:
            f.write(r.content)
    except Exception as e:
        print(f"Error downloading DB: {e}")

def sync_db_to_cloud():
    """Upload the updated database from /tmp back to Vercel Blob using plain HTTP."""
    try:
        with open(TMP_DB_PATH, "rb") as f:
            files = {"file": (BLOB_FILENAME, f, "application/x-sqlite3")}
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "x-api-version": "1",
            }
            r = requests.post(
                BLOB_API,
                headers=headers,
                files=files,
                data={"access": "public"},  # or "private" if you prefer
                timeout=30,
            )
            r.raise_for_status()
    except Exception as e:
        print(f"Error uploading DB: {e}")

# Create App instance pointing to the /tmp database
paths = Paths()
paths.live_db = TMP_DB_PATH
paths.sample_db = TMP_DB_PATH
app_instance = App(paths)

def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    query = parse_qs(environ.get("QUERY_STRING", ""))
    which = "live"

    sync_db_from_cloud()

    if path.startswith("/api/"):
        if method == "GET":
            if path == "/api/status": data = app_instance.status()
            elif path == "/api/models": data = app_instance.models(which)
            elif path == "/api/quality": data = app_instance.quality(which)
            elif path == "/api/report": data = app_instance.report(which)
            else:
                start_response("404 Not Found", [("Content-Type", "text/plain")])
                return [b"API Not Found"]

            if data is None:
                start_response("404 Not Found", [("Content-Type", "application/json")])
                return [b'{"error": "no data yet - run ETL"}']

            res = json.dumps(data, default=str).encode("utf-8")
            start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(res)))])
            return [res]

        elif method == 'POST':
            if path == '/api/run':
                try:
                    # Trigger your pipeline
                    from aimodels.pipeline import run_etl
                    import pathlib
                    
                    # WE ADDED raw_dir='/tmp/raw' HERE 👇
                    summary = run_etl(
                        offline=False, 
                        use_cache=False, 
                        skip_hf=False, 
                        db_path=TMP_DB_PATH,
                        raw_dir=pathlib.Path("/tmp/raw") 
                    )
                    
                    # Save the new database to the cloud
                    sync_db_to_cloud()
                    
                    res = json.dumps({"status": "done", "n_valid": summary.n_valid}).encode('utf-8')
                    start_response('200 OK', [('Content-Type', 'application/json')])
                    return [res]
                except Exception as e:
                    start_response('500 Internal Error', [('Content-Type', 'application/json')])
                    return [json.dumps({"error": str(e)}).encode('utf-8')]

    # Serve Frontend
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
