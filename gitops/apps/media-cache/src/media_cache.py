"""media-cache: caches transcoded thumbnails on local scratch space and never evicts them.

Fills /cache with FILE_MB files at FILL_MBPS until it holds FILL_MB, then keeps serving.
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILL_MB = int(os.environ.get("FILL_MB", "950"))
FILL_MBPS = float(os.environ.get("FILL_MBPS", "10"))
FILE_MB = int(os.environ.get("FILE_MB", "8"))
CACHE = os.environ.get("CACHE", "/cache")


def fill() -> None:
    chunk = os.urandom(FILE_MB << 20)
    n, last = 0, 0.0
    while True:
        held = sum(e.stat().st_size for e in os.scandir(CACHE) if e.is_file()) >> 20
        if held + FILE_MB <= FILL_MB:
            with open(os.path.join(CACHE, f"thumb-{n:05d}.bin"), "wb") as f:
                f.write(chunk)
            n += 1
        if time.monotonic() - last >= 10:
            print(f"cache holds {held} MiB in {n} files (never evicted)", flush=True)
            last = time.monotonic()
        time.sleep(FILE_MB / FILL_MBPS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=fill, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
