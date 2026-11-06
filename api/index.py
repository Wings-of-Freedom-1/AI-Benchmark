import sys
import os
import json
from urllib.parse import parse_qs

# 1. Add the 'src' directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from aimodels.dashboard import App, Paths

# 2. Create a single instance of your app
app_instance = App(Paths())

# 3. The WSGI application
def app(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    method = environ.get('REQUEST_METHOD', 'GET')
    query = parse_qs(environ.get('QUERY_STRING', ''))
    
    # Determine which dataset is being requested
    which = "live" if query.get("db", ["sample"])[0] == "live" else "sample"

    # --- SERVE API ROUTES ---
    if path.startswith('/api/'):
        if method == 'GET':
            if path == '/api/status':
                data = app_instance.status()
            elif path == '/api/models':
                data = app_instance.models(which)
            elif path == '/api/quality':
                data = app_instance.quality(which)
            elif path == '/api/report':
                data = app_instance.report(which)
            else:
                start_response('404 Not Found', [('Content-Type', 'text/plain')])
                return [b'API Not Found']

            if data is None:
                start_response('404 Not Found', [('Content-Type', 'application/json')])
                return [b'{"error": "no data yet - check your database file"}']
            
            # Serve JSON response
            res = json.dumps(data, default=str).encode('utf-8')
            start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(res)))])
            return [res]

        # Block POST requests for now (prevents running ETL on Vercel)
        start_response('405 Method Not Allowed', [('Content-Type', 'text/plain')])
        return [b'POST requests are disabled on the public server.']

    # --- SERVE FRONTEND (HTML & STATIC FILES) ---
    web_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'aimodels', 'web'))
    
    if path == '/' or path == '/index.html':
        filepath = os.path.join(web_dir, 'index.html')
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                # Inject the token so the dashboard can make API calls
                html = f.read().replace('__TOKEN__', app_instance.token)
            res = html.encode('utf-8')
            start_response('200 OK', [('Content-Type', 'text/html; charset=utf-8'), ('Content-Length', str(len(res)))])
            return [res]
    
    if path.startswith('/static/'):
        filename = path.replace('/static/', '')
        filepath = os.path.join(web_dir, filename)
        if os.path.exists(filepath) and os.path.isfile(filepath):
            with open(filepath, 'rb') as f:
                content = f.read()
            
            ctype = 'application/octet-stream'
            if filename.endswith('.css'): ctype = 'text/css'
            elif filename.endswith('.js'): ctype = 'application/javascript'
            
            start_response('200 OK', [('Content-Type', ctype), ('Content-Length', str(len(content)))])
            return [content]

    # Default 404
    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
