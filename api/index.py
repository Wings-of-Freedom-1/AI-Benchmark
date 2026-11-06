import sys
import os
import json
from urllib.parse import parse_qs
from vercel_blob import put, list

# Add the 'src' directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from aimodels.dashboard import App, Paths

# Define where Vercel is allowed to write
TMP_DB_PATH = "/tmp/models.db"
BLOB_FILENAME = "models.db"

def sync_db_from_cloud():
    """Download the latest database from Vercel Blob to /tmp"""
    if os.path.exists(TMP_DB_PATH):
        return  # Already downloaded for this serverless instance
    try:
        blobs = list({"prefix": BLOB_FILENAME})
        if blobs:
            import requests
            r = requests.get(blobs[0]['url'])
            with open(TMP_DB_PATH, 'wb') as f:
                f.write(r.content)
    except Exception as e:
        print(f"Error downloading DB: {e}")

def sync_db_to_cloud():
    """Upload the updated database from /tmp back to Vercel Blob"""
    try:
        with open(TMP_DB_PATH, 'rb') as f:
            put(BLOB_FILENAME, f.read(), {"access": "public", "allowOverwrite": True})
    except Exception as e:
        print(f"Error uploading DB: {e}")

# Create App instance pointing to the /tmp database
paths = Paths()
paths.live_db = TMP_DB_PATH
paths.sample_db = TMP_DB_PATH
app_instance = App(paths)

def app(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    method = environ.get('REQUEST_METHOD', 'GET')
    query = parse_qs(environ.get('QUERY_STRING', ''))
    which = "live"

    # Sync database from cloud on first request
    sync_db_from_cloud()

    # --- SERVE API ROUTES ---
    if path.startswith('/api/'):
        if method == 'GET':
            if path == '/api/status': data = app_instance.status()
            elif path == '/api/models': data = app_instance.models(which)
            elif path == '/api/quality': data = app_instance.quality(which)
            elif path == '/api/report': data = app_instance.report(which)
            else:
                start_response('404 Not Found', [('Content-Type', 'text/plain')])
                return [b'API Not Found']

            if data is None:
                start_response('404 Not Found', [('Content-Type', 'application/json')])
                return [b'{"error": "no data yet - run ETL"}']
            
            res = json.dumps(data, default=str).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(res)))])
            return [res]

        elif method == 'POST':
            # Allow the "Run ETL" button to work!
            if path == '/api/run':
                try:
                    length = int(environ.get('CONTENT_LENGTH', 0))
                    body = json.loads(environ['wsgi.input'].read(length))
                    
                    # Trigger your pipeline
                    from aimodels.pipeline import run_etl
                    summary = run_etl(offline=False, use_cache=False, skip_hf=False, db_path=TMP_DB_PATH)
                    
                    # Save the new database to the cloud
                    sync_db_to_cloud()
                    
                    res = json.dumps({"status": "done", "n_valid": summary.n_valid}).encode('utf-8')
                    start_response('200 OK', [('Content-Type', 'application/json')])
                    return [res]
                except Exception as e:
                    start_response('500 Internal Error', [('Content-Type', 'application/json')])
                    return [json.dumps({"error": str(e)}).encode('utf-8')]

            start_response('404 Not Found', [('Content-Type', 'text/plain')])
            return [b'Not Found']

    # --- SERVE FRONTEND ---
    web_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'aimodels', 'web'))
    if path == '/' or path == '/index.html':
        filepath = os.path.join(web_dir, 'index.html')
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                html = f.read().replace('__TOKEN__', app_instance.token)
            res = html.encode('utf-8')
            start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(res)))])
            return [res]
    
    if path.startswith('/static/'):
        filename = path.replace('/static/', '')
        filepath = os.path.join(web_dir, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, 'rb') as f: content = f.read()
            ctype = 'text/css' if filename.endswith('.css') else 'application/javascript' if filename.endswith('.js') else 'application/octet-stream'
            start_response('200 OK', [('Content-Type', ctype), ('Content-Length', str(len(content)))])
            return [content]

    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
