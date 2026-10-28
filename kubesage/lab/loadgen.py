"""Open-loop HTTP load for the latency test case: RPS requests per second to TARGET for SECONDS."""
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TARGET, RPS, SECONDS = os.environ["TARGET"], float(os.environ.get("RPS", "25")), float(os.environ.get("SECONDS", "600"))
lat, errors, lock = [], [0], threading.Lock()


def hit():
    started = time.monotonic()
    try:
        urllib.request.urlopen(TARGET, timeout=30).read()
        with lock:
            lat.append(time.monotonic() - started)
    except Exception:
        with lock:
            errors[0] += 1


print(f"loadgen: {RPS:g} rps -> {TARGET} for {SECONDS:g}s", flush=True)
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
