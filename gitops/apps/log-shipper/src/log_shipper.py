"""log-shipper: spools audit logs to local disk before shipping them, fsyncing every block for durability.

Writes WRITE_MBPS MB/s in BLOCK_KB blocks into /data, rotating SEGMENT_MB files and keeping KEEP of them.
"""
import os
import time

WRITE_MBPS = float(os.environ.get("WRITE_MBPS", "20"))
BLOCK_KB = int(os.environ.get("BLOCK_KB", "256"))
SEGMENT_MB = int(os.environ.get("SEGMENT_MB", "64"))
KEEP = int(os.environ.get("KEEP", "4"))
DATA = os.environ.get("DATA", "/data")

if __name__ == "__main__":
    block = os.urandom(BLOCK_KB << 10)
    per_segment = (SEGMENT_MB << 10) // BLOCK_KB
    segment, written, last, start = 0, 0, time.monotonic(), time.monotonic()
    print(f"log-shipper: {WRITE_MBPS} MB/s in {BLOCK_KB} KiB fsynced blocks -> {DATA}", flush=True)
    while True:
        path = os.path.join(DATA, f"spool-{segment % KEEP}.log")
        with open(path, "wb") as f:
            for _ in range(per_segment):
                f.write(block)
                f.flush()
                os.fsync(f.fileno())
                written += len(block)
                behind = written / (WRITE_MBPS * 1e6) - (time.monotonic() - start)
                if behind > 0:
                    time.sleep(behind)
                if (now := time.monotonic()) - last >= 10:
                    print(f"spooled {written / 1e6:,.0f} MB, {written / 1e6 / (now - start):.1f} MB/s", flush=True)
                    last = now
        segment += 1
