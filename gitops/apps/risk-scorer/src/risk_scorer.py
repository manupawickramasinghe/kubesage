"""risk-scorer: scores payment transactions for fraud risk, in batches, on a pool of WORKERS.

Scoring is pure-Python arithmetic, so it is CPU-bound.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

WORKERS = int(os.environ.get("WORKERS", "3"))
BATCH = int(os.environ.get("BATCH", "20000"))


def score(batch: int) -> float:
    risk = 0.0
    for txn in range(BATCH):
        amount = (txn * 7919 + batch) % 10_000
        risk += (amount * amount) % 97 / 97
    return risk / BATCH


if __name__ == "__main__":
    print(f"risk-scorer: {WORKERS} workers, {BATCH} transactions per batch", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        batch, done, last = 0, 0, time.monotonic()
        while True:
            futures = [pool.submit(score, batch + i) for i in range(WORKERS * 4)]
            for future in futures:
                future.result()
            batch += len(futures)
            done += len(futures) * BATCH
            if (now := time.monotonic()) - last >= 10:
                print(f"transactions/s={done / (now - last):,.0f} workers={WORKERS}", flush=True)
                done, last = 0, now
