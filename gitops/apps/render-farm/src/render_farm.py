"""render-farm: renders frames on WORKERS processes, as fast as the CPU allows.

The work is CPU-bound and parallel, so it uses every core its CPU limit grants it; a tight limit
shows up as CFS throttling and fewer frames per second.
"""
import itertools
import os
import time
from multiprocessing import Pool

WORKERS = int(os.environ.get("WORKERS", "4"))
FRAME_WORK = int(os.environ.get("FRAME_WORK", "200000"))


def render(frame: int) -> int:
    acc = 0
    for i in range(FRAME_WORK):
        acc = (acc + i * frame) % 1_000_003
    return acc


if __name__ == "__main__":
    print(f"render-farm: {WORKERS} worker processes", flush=True)
    with Pool(WORKERS) as pool:
        done, last = 0, time.monotonic()
        for _ in pool.imap_unordered(render, itertools.count(), chunksize=4):
            done += 1
            if (now := time.monotonic()) - last >= 10:
                print(f"frames/s={done / (now - last):.1f} workers={WORKERS}", flush=True)
                done, last = 0, now
