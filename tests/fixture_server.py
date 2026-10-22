"""HTTP fixture for the Docker smoke test: flaky services, a slow endpoint, and a webhook receiver."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time


class Endpoint(BaseHTTPRequestHandler):
    # Each flaky path answers 503 to its first two checks and 204 after that.
    checks = {"/service": 0, "/blip": 0}
    alerts = []

    def do_POST(self):
        if self.path != "/webhook":
            return self.reply(404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Keep the header beside the body so the smoke test can compare them.
        Endpoint.alerts.append({**body, "header_key": self.headers.get("Idempotency-Key")})
        self.reply(204)

    def do_GET(self):
        if self.path == "/alerts":
            return self.reply(200, json.dumps(Endpoint.alerts).encode())
        if self.path == "/health":
            status = 200
        elif self.path == "/slow":
            time.sleep(3)
            status = 200
        elif self.path in Endpoint.checks:
            Endpoint.checks[self.path] += 1
            status = 503 if Endpoint.checks[self.path] <= 2 else 204
        else:
            status = 404
        self.reply(status)

    def reply(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Endpoint).serve_forever()
