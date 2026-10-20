"""Local servers that misbehave on cue, for the reliability scenarios.

`Faults` answers `/<target>` according to a schedule of fault names, and
`Receiver` accepts webhook POSTs or fails them. Both read the scenario's current
round from a shared `Clock`, so each request gets the fault planned for it.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

# What each fault should be saved as: (state, error).
EXPECTED = {
    "ok": ("up", None),
    "error": ("down", "http_503"),
    "redirect": ("down", "http_302"),
    "reset": ("down", "connection_error"),
    "garbage": ("down", "connection_error"),
    "slow": ("down", "timeout"),
    "trickle": ("down", "deadline_exceeded"),
    "dns": ("down", "deadline_exceeded"),
}
SLOW_SECONDS = 0.6
TRICKLE_GAP = 0.04
DNS_HANG_SECONDS = 2.0


class Clock:
    """The scenario's current round, shared with the servers' request threads."""

    def __init__(self):
        self.round = 0
        # Set on cleanup so held connections and hung lookups end at once.
        self.stopped = threading.Event()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, handler, clock):
        super().__init__(("127.0.0.1", 0), handler)
        self.clock = clock
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.clock.stopped.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class Faults(_Server):
    """Serve `/<target>` with the fault `schedule(round, target)` returns."""

    def __init__(self, clock, schedule):
        super().__init__(_FaultHandler, clock)
        self.schedule = schedule


class _FaultHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        fault = self.server.schedule(self.server.clock.round, self.path.lstrip("/"))
        stopped = self.server.clock.stopped
        try:
            if fault == "reset":
                self.close_connection = True
                return
            if fault == "garbage":
                self.wfile.write(b"NOT HTTP\r\n\r\n")
                return
            if fault == "slow" and stopped.wait(SLOW_SECONDS):
                return
            if fault == "trickle":
                # Each byte arrives well inside the socket timeout, so only a deadline ends the wait.
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    if stopped.wait(TRICKLE_GAP):
                        return
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                return
            status = {"error": 503, "redirect": 302}.get(fault, 200)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


class Receiver(_Server):
    """Accept webhook POSTs when `mode(round)` is "up"; otherwise fail them.

    "error" answers 500 and "reset" drops the connection. Every body that was read,
    accepted or not, is kept in `received` with the round it arrived in.
    `before_reply`, if set, runs after a body is read and before any answer.
    """

    def __init__(self, clock, mode):
        super().__init__(_ReceiverHandler, clock)
        self.mode = mode
        self.received = []
        self.before_reply = None
        self.lock = threading.Lock()

    def accepted(self) -> list[dict]:
        with self.lock:
            return [body for body, _round, accepted in self.received if accepted]


class _ReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        current = self.server.clock.round
        mode = self.server.mode(current)
        if self.server.before_reply is not None:
            self.server.before_reply(body)
        with self.server.lock:
            self.server.received.append((body, current, mode == "up"))
        if mode == "reset":
            self.close_connection = True
            return
        try:
            self.send_response(204 if mode == "up" else 500)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def resolver(clock, schedule):
    """Build a `getaddrinfo` that resolves `<target>.reliability.test` to 127.0.0.1.

    A lookup hangs when the schedule says "dns" for that target and round, the way a
    stuck resolver does; other names resolve normally.
    """
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".reliability.test"):
            if schedule(clock.round, host.split(".")[0]) == "dns":
                clock.stopped.wait(DNS_HANG_SECONDS)
                raise socket.gaierror("lookup timed out")
            return real("127.0.0.1", *args, **kwargs)
        return real(host, *args, **kwargs)

    return getaddrinfo
