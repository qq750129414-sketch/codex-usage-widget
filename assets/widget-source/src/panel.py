"""Loopback-only, read-only view of the widget's existing in-memory snapshot."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

PORT = 49672
ASSETS = {"/": ("panel.html", "text/html; charset=utf-8"),
          "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
          "/panel.css": ("panel.css", "text/css; charset=utf-8")}


def safe_text(value):
    text = str(value or "")
    text = re.sub(r"rr(?:_|\\_)live(?:_|\\_)[A-Za-z0-9_\\-]+", "[密钥已隐藏]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[密钥已隐藏]", text)


def public_snapshot(data):
    """Only the fields required for display; never export raw logs or credentials."""
    runs = []
    for r in data.get("runs", []):
        item = {k: r.get(k) for k in ("id", "thread", "sent", "started", "ended", "duration")}
        item.update({k: safe_text(r.get(k)) for k in ("title", "model", "effort", "status")})
        item["title"] = item["title"].split("<image", 1)[0].strip() or "附件消息"
        item["tokens"] = r.get("usage", {}).get("total_tokens")
        runs.append(item)
    buckets = []
    for b in data.get("buckets", []):
        if b.get("id") != "codex":
            continue
        item = {"id": "codex", "planType": safe_text(b.get("planType")), "planLabel": safe_text(b.get("planLabel"))}
        for name in ("primary", "secondary"):
            q = b.get(name)
            item[name] = {k: q.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt")} if q else None
        buckets.append(item)
    radar = data.get("radar") or {}
    return {"checked": data.get("checked"), "quotaUpdated": data.get("quotaUpdated"),
            "quotaError": safe_text(data.get("quotaError")), "buckets": buckets, "runs": runs,
            "radar": {"headline": safe_text(radar.get("headline")),
                      "resetReference": safe_text(radar.get("resetReference")),
                      "details": [safe_text(x) for x in radar.get("details", [])]}}


class PanelServer:
    def __init__(self, port=PORT, assets=None):
        self.assets = assets or Path(__file__).with_name("web")
        self.payload = b'{"checked":null,"runs":[],"buckets":[]}'
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *_):
                pass

            def send_body(self, status, body, mime):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                authority = "127.0.0.1:" + str(owner.http.server_port)
                if self.headers.get("Host") != authority:
                    return self.send_body(403, b"Forbidden", "text/plain")
                if self.headers.get("Origin") not in (None, "http://" + authority):
                    return self.send_body(403, b"Forbidden", "text/plain")
                path = urlsplit(self.path).path
                if path == "/snapshot":
                    if self.headers.get("X-Usage-Widget") != "read" or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                        return self.send_body(403, b"Forbidden", "text/plain")
                    with owner.lock:
                        payload = owner.payload
                    return self.send_body(200, payload, "application/json; charset=utf-8")
                if path == "/health":
                    return self.send_body(200, b'{"service":"codex-usage-panel","ok":true}', "application/json")
                if path not in ASSETS:
                    return self.send_body(404, b"Not found", "text/plain")
                filename, mime = ASSETS[path]
                self.send_body(200, (owner.assets / filename).read_bytes(), mime)

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http.daemon_threads = True

    def start(self):
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def publish(self, data):
        payload = json.dumps(public_snapshot(data), ensure_ascii=False, allow_nan=False).encode()
        with self.lock:
            self.payload = payload

    def close(self):
        self.http.shutdown()
        self.http.server_close()
