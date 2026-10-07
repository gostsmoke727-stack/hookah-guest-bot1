import os
import runpy
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "10000"))

class Handler(BaseHTTPRequestHandler):
    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in {"/webapp", "/webapp/"}:
            path = "/webapp/index.html"
        if path == "/webapp/index.html":
            try:
                body = (os.path.dirname(os.path.abspath(__file__)) + "/webapp/index.html")
                with open(body, "rb") as f:
                    data = f.read()
                self._send(data, "text/html; charset=utf-8")
            except OSError:
                self._send(b"Mini App file is unavailable", "text/plain; charset=utf-8", 500)
            return
        self._send(b'{"status":"ok","service":"hookah-guest-bot1"}', "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, format, *args):
        return

server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
threading.Thread(target=server.serve_forever, name="render-health", daemon=True).start()
print(f"Health server listening on 0.0.0.0:{PORT}", flush=True)

os.environ["RENDER_HEALTH_ALREADY_RUNNING"] = "1"
runpy.run_module("bot", run_name="__main__")
