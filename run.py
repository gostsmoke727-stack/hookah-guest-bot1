import os
import runpy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "10000"))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","service":"hookah-guest-bot1"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return

server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
threading.Thread(target=server.serve_forever, name="render-health", daemon=True).start()
print(f"Health server listening on 0.0.0.0:{PORT}", flush=True)

os.environ["RENDER_HEALTH_ALREADY_RUNNING"] = "1"
runpy.run_module("bot", run_name="__main__")
