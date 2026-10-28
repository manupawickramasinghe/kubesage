"""api-gateway: the storefront's API edge. Each request does WORK_MS of CPU (auth, routing, JSON).

Exports request latency as a Prometheus histogram on /metrics, which vmagent scrapes.
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORK_MS = float(os.environ.get("WORK_MS", "30"))
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
counts = [0] * (len(BUCKETS) + 1)
total = [0.0, 0]
lock = threading.Lock()


def burn(seconds: float) -> None:
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def observe(seconds: float) -> None:
    with lock:
        for i, le in enumerate(BUCKETS):
            if seconds <= le:
                counts[i] += 1
        counts[-1] += 1
        total[0] += seconds
        total[1] += 1


def metrics() -> bytes:
    with lock:
        lines = ["# TYPE http_request_duration_seconds histogram"]
        lines += [f'http_request_duration_seconds_bucket{{le="{le}"}} {counts[i]}' for i, le in enumerate(BUCKETS)]
        lines += [f'http_request_duration_seconds_bucket{{le="+Inf"}} {counts[-1]}',
                  f"http_request_duration_seconds_sum {total[0]:.6f}", f"http_request_duration_seconds_count {total[1]}"]
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started = time.monotonic()
        if self.path.startswith("/api"):
            burn(WORK_MS / 1000)
            body = b"ok\n"
        elif self.path == "/metrics":
            body = metrics()
        elif self.path == "/healthz":
            body = b"ok\n"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if self.path.startswith("/api"):
            observe(time.monotonic() - started)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
