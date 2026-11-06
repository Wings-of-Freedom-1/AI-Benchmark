import sys
import os

# 1. Add the 'src' directory to the Python path so Vercel can find 'aimodels'
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

# 2. Import your existing App class
from aimodels.dashboard import App, Paths

# 3. Create a single instance of your app
app_instance = App(Paths())

# 4. Vercel expects a top-level variable named 'app'. 
# This WSGI function will handle incoming requests.
def app(environ, start_response):
    path = environ.get('PATH_INFO', '/')
    
    # A minimal router for testing. Add more routes as needed.
    if path == '/api/status':
        import json
        data = json.dumps(app_instance.status()).encode('utf-8')
        status = '200 OK'
        headers = [('Content-Type', 'application/json'), ('Content-Length', str(len(data)))]
        start_response(status, headers)
        return [data]
    
    # Default 404 response
    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
