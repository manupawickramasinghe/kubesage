"""Open-loop HTTP load for the latency test case: RPS requests per second to TARGET for SECONDS.

It is also the client-side probe: /metrics on :9102 exports what callers actually wait, queueing included
(probe_http_duration_seconds{target=...}). A starved server can't measure the time requests spend waiting to be
accepted; its own histogram only starts once a thread picks the request up. The probe's own queue counts too:
latency runs from the moment a request was due, not from when a worker thread got to it.

Open loop with a bound: at most MAX_INFLIGHT requests are outstanding. A request due while that many are still
waiting is dropped and counted (drop=), so a server that stops answering can't grow an unbounded backlog, and
the job ends within a drain deadline of SECONDS instead of whenever the backlog clears.
"""
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET, RPS, SECONDS = os.environ["TARGET"], float(os.environ.get("RPS", "25")), float(os.environ.get("SECONDS", "600"))
NAME = os.environ.get("TARGET_APP", "target")
MAX_INFLIGHT, TIMEOUT = int(os.environ.get("MAX_INFLIGHT", "200")), float(os.environ.get("TIMEOUT", "30"))
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
counts, total, failures = [0] * (len(BUCKETS) + 1), [0.0, 0], [0]
lat, waits, errors, dropped, inflight, lock = [], [], [0], [0, 0], [0], threading.Lock()  # dropped: [window, total]


def hit(due: float):
    picked = time.monotonic()
    try:
        urllib.request.urlopen(TARGET, timeout=TIMEOUT).read()
        took = time.monotonic() - due
        with lock:
            waits.append(picked - due)
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
    finally:
        with lock:
            inflight[0] -= 1


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            t = f'target="{NAME}"'
            lines = [f'probe_http_duration_seconds_bucket{{{t},le="{le}"}} {counts[i]}' for i, le in enumerate(BUCKETS)]
            lines += [f'probe_http_duration_seconds_bucket{{{t},le="+Inf"}} {counts[-1]}',
                      f"probe_http_duration_seconds_sum{{{t}}} {total[0]:.6f}", f"probe_http_duration_seconds_count{{{t}}} {total[1]}",
                      f"probe_http_failures_total{{{t}}} {failures[0]}", f"probe_http_dropped_total{{{t}}} {dropped[1]}"]
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


threading.Thread(target=ThreadingHTTPServer(("", int(os.environ.get("METRICS_PORT", "9102"))), Metrics).serve_forever,
                 daemon=True).start()
print(f"loadgen: {RPS:g} rps -> {TARGET} for {SECONDS:g}s; client-side latency on :9102/metrics", flush=True)
def report() -> None:
    with lock:
        xs, ws, errs, drops = sorted(lat), sorted(waits), errors[0], dropped[0]
        lat.clear()
        waits.clear()
        errors[0] = dropped[0] = 0
    pct = lambda v: v[int(0.95 * (len(v) - 1))] * 1000 if v else 0
    print(f"ok={len(xs)} err={errs} drop={drops} p95={pct(xs):.0f}ms queue_p95={pct(ws):.0f}ms", flush=True)


pool, start, sent, last = ThreadPoolExecutor(max_workers=MAX_INFLIGHT), time.monotonic(), 0, time.monotonic()
while (now := time.monotonic()) - start < SECONDS:
    due = int((now - start) * RPS)
    for n in range(sent, due):
        with lock:
            full = inflight[0] >= MAX_INFLIGHT
            if full:
                dropped[0] += 1
                dropped[1] += 1
            else:
                inflight[0] += 1
        if not full:
            pool.submit(hit, start + n / RPS)
    sent = due
    if now - last >= 10:
        report()
        last = now
    time.sleep(0.005)
# Stop on time: nothing new is sent, and what is still waiting gets at most one request timeout to finish.
# (Every submitted request already has a worker, since no more than MAX_INFLIGHT are ever outstanding.)
pool.shutdown(wait=False)
deadline = time.monotonic() + TIMEOUT + 5
while inflight[0] > 0 and time.monotonic() < deadline:
    time.sleep(0.1)
report()
print(f"loadgen: done after {time.monotonic() - start:.0f}s; {inflight[0]} request(s) abandoned at the deadline", flush=True)
os._exit(0)  # don't wait for worker threads stuck past the deadline
