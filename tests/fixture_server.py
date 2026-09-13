from http.server import BaseHTTPRequestHandler, HTTPServer


class Endpoint(BaseHTTPRequestHandler):
    checks = 0

    def do_GET(self):
        if self.path == "/health":
            status = 200
        elif self.path == "/service":
            Endpoint.checks += 1
            status = 503 if Endpoint.checks <= 2 else 204
        else:
            status = 404
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Endpoint).serve_forever()
