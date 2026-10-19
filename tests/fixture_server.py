from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time


class Endpoint(BaseHTTPRequestHandler):
    checks = 0
    alerts = []

    def do_POST(self):
        if self.path != "/webhook":
            return self.reply(404)
        Endpoint.alerts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.reply(204)

    def do_GET(self):
        if self.path == "/alerts":
            return self.reply(200, json.dumps(Endpoint.alerts).encode())
        if self.path == "/health":
            status = 200
        elif self.path == "/slow":
            time.sleep(3)
            status = 200
        elif self.path == "/service":
            Endpoint.checks += 1
            status = 503 if Endpoint.checks <= 2 else 204
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
