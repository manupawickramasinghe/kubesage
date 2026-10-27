"""KubeSage demo workload: a steady baseline CPU burn plus an HTTP endpoint that burns CPU per request.

Mounted from a ConfigMap into python:alpine; stdlib only.
  python app.py serve     BASELINE_CPU=0.05 PORT=8080
  python app.py loadgen   TARGET=http://checkout-web:8080 RPS=40 WORK_MS=50 SECONDS=180
Requests are spread over the Service's pods, so scaling out really does lower per-pod CPU.
"""
import os
import signal
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def burn(seconds: float) -> None:
    """Spin for `seconds` of CPU time (not wall time), so CFS throttling makes requests slower, not cheaper."""
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def baseline(cores: float, period: float = 0.1) -> None:
    while cores > 0:
        start = time.monotonic()
        burn(cores * period)
        time.sleep(max(0.0, period - (time.monotonic() - start)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/work":
            burn(min(float(parse_qs(url.query).get("ms", ["50"])[0]), 5000) / 1000)
        elif url.path != "/healthz":
            self.send_error(404)
            return
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(port: int) -> ThreadingHTTPServer:
    threading.Thread(target=baseline, args=(float(os.environ.get("BASELINE_CPU", "0")),), daemon=True).start()
    return ThreadingHTTPServer(("", port), Handler)


def loadgen() -> None:
    url = f"{os.environ.get('TARGET', 'http://checkout-web:8080')}/work?ms={os.environ.get('WORK_MS', '50')}"
    rps, seconds = float(os.environ.get("RPS", "40")), float(os.environ.get("SECONDS", "180"))
    latencies, errors, lock = [], [0], threading.Lock()

    def hit():
        started = time.monotonic()
        try:
            urllib.request.urlopen(url, timeout=30).read()
            with lock:
                latencies.append(time.monotonic() - started)
        except Exception:
            with lock:
                errors[0] += 1

    def report(elapsed):
        with lock:
            lat, errs = sorted(latencies), errors[0]
            latencies.clear()
            errors[0] = 0
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] * 1000 if lat else 0.0
        print(f"t={elapsed:4.0f}s ok={len(lat)} err={errs} p50={p(0.5):.0f}ms p95={p(0.95):.0f}ms", flush=True)

    print(f"loadgen: {rps:g} rps x {os.environ.get('WORK_MS', '50')} ms CPU -> {url} for {seconds:g}s", flush=True)
    pool = ThreadPoolExecutor(max_workers=256)
    start = last_report = time.monotonic()
    sent = 0
    while (now := time.monotonic()) - start < seconds:
        due = int((now - start) * rps)
        for _ in range(due - sent):
            pool.submit(hit)
        sent = due
        if now - last_report >= 10:
            report(now - start)
            last_report = now
        time.sleep(0.005)
    pool.shutdown(wait=True)
    report(time.monotonic() - start)


if __name__ == "__main__":
    # As PID 1 the default SIGTERM action is ignored; exit promptly so scale-down and migration look instant.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if sys.argv[1:2] == ["loadgen"]:
        loadgen()
    else:
        serve(int(os.environ.get("PORT", "8080"))).serve_forever()
