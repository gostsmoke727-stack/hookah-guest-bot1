"""Render web-service compatibility shim for the Telegram polling bot.

Render's free Web Service expects an HTTP listener. The bot itself is a long-running
Telegram polling process, so this tiny health endpoint lets the same process satisfy
Render's port check without changing the bot's Telegram logic.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _run_health_server():
    try:
        port = int(os.getenv("PORT", "10000"))
    except ValueError:
        port = 10000

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

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        print(f"Health server listening on 0.0.0.0:{port}", flush=True)
        server.serve_forever()
    except Exception as exc:
        print(f"Health server failed: {exc}", flush=True)


threading.Thread(target=_run_health_server, name="render-health", daemon=True).start()
