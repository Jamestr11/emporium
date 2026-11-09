"""WSGI entry point, for hosts like PythonAnywhere that run Python web apps through WSGI.

It reuses server.py's request handling unchanged: each WSGI request is fed through the
same Handler, with its output captured instead of written to a socket.
Settings (ADMIN_PASSWORD, SECRET_KEY, DB_PATH, ...) come from environment variables;
set them before importing this module.
"""
import io
from email.message import Message

import server

server.init_db()


class _WSGIHandler(server.Handler):
    def __init__(self, environ):  # deliberately skips BaseHTTPRequestHandler.__init__
        self.environ = environ
        self.command = environ["REQUEST_METHOD"].upper()
        query = environ.get("QUERY_STRING", "")
        self.path = (environ.get("PATH_INFO") or "/") + ("?" + query if query else "")
        self.headers = Message()
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                self.headers[key[5:].replace("_", "-").title()] = value
        for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if environ.get(key):
                self.headers[key.replace("_", "-").title()] = environ[key]
        self.rfile = environ["wsgi.input"]
        self.wfile = io.BytesIO()
        self.status = "500 Internal Server Error"
        self.out_headers = []

    def send_response(self, code, message=None):
        from http import HTTPStatus
        try:
            phrase = message or HTTPStatus(code).phrase
        except ValueError:
            phrase = ""
        self.status = f"{code} {phrase}"

    def send_header(self, keyword, value):
        self.out_headers.append((keyword, str(value)))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        pass


def application(environ, start_response):
    h = _WSGIHandler(environ)
    method = h.command if h.command in ("GET", "POST", "PATCH", "DELETE") else None
    if method:
        h.handle_any(method)
    else:
        h.send_json(405, {"error": "Method not allowed."})
    start_response(h.status, h.out_headers)
    return [h.wfile.getvalue()]


if __name__ == "__main__":
    # Local check: python3 wsgi.py  → http://localhost:8001
    from wsgiref.simple_server import make_server
    print("WSGI test server on http://localhost:8001")
    make_server("", 8001, application).serve_forever()
