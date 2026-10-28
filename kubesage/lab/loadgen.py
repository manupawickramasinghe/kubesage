"""Open-loop HTTP load for the latency test case: RPS requests per second to TARGET for SECONDS.

It is also the client-side probe: /metrics on :9102 exports what callers actually wait, queueing included
(probe_http_duration_seconds{target=...}). A starved server can't measure the time requests spend waiting to be
accepted; its own histogram only starts once a thread picks the request up.
"""
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET, RPS, SECONDS = os.environ["TARGET"], float(os.environ.get("RPS", "25")), float(os.environ.get("SECONDS", "600"))
NAME = os.environ.get("TARGET_APP", "target")
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
counts, total, failures = [0] * (len(BUCKETS) + 1), [0.0, 0], [0]
lat, errors, lock = [], [0], threading.Lock()


def hit():
    started = time.monotonic()
    try:
        urllib.request.urlopen(TARGET, timeout=30).read()
        took = time.monotonic() - started
        with lock:
            lat.append(took)
            for i, le in enumerate(BUCKETS):
                if took <= le:
                    counts[i] += 1
            counts[-1] += 1
            total[0] += took
            total[1] += 1
    except Exception:
        with lock:
            errors[0] += 1
            failures[0] += 1


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            t = f'target="{NAME}"'
            lines = [f'probe_http_duration_seconds_bucket{{{t},le="{le}"}} {counts[i]}' for i, le in enumerate(BUCKETS)]
            lines += [f'probe_http_duration_seconds_bucket{{{t},le="+Inf"}} {counts[-1]}',
                      f"probe_http_duration_seconds_sum{{{t}}} {total[0]:.6f}", f"probe_http_duration_seconds_count{{{t}}} {total[1]}",
                      f"probe_http_failures_total{{{t}}} {failures[0]}"]
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


threading.Thread(target=ThreadingHTTPServer(("", 9102), Metrics).serve_forever, daemon=True).start()
print(f"loadgen: {RPS:g} rps -> {TARGET} for {SECONDS:g}s; client-side latency on :9102/metrics", flush=True)
pool, start, sent, last = ThreadPoolExecutor(max_workers=200), time.monotonic(), 0, time.monotonic()
while (now := time.monotonic()) - start < SECONDS:
    due = int((now - start) * RPS)
    for _ in range(due - sent):
        pool.submit(hit)
    sent = due
    if now - last >= 10:
        with lock:
            xs, errs = sorted(lat), errors[0]
            lat.clear()
            errors[0] = 0
        p95 = xs[int(0.95 * (len(xs) - 1))] * 1000 if xs else 0
        print(f"ok={len(xs)} err={errs} p95={p95:.0f}ms", flush=True)
        last = now
    time.sleep(0.005)
