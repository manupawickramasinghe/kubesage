"""ledger-cache: keeps the whole day's ledger in memory so lookups never touch disk.

It loads the ledger in CHUNK_MB blocks until it holds TARGET_MB, then serves from RAM. Every page is touched,
so the memory is really resident (and really counted against the container's limit).
"""
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK_MB = int(os.environ.get("CHUNK_MB", "16"))
TARGET_MB = int(os.environ.get("TARGET_MB", "700"))
EVERY_S = float(os.environ.get("EVERY_S", "2"))
ledger: list[bytearray] = []


def load() -> None:
    while True:
        if len(ledger) * CHUNK_MB < TARGET_MB:
            block = bytearray(CHUNK_MB << 20)
            for i in range(0, len(block), 4096):
                block[i] = 1
            ledger.append(block)
        print(f"ledger resident={len(ledger) * CHUNK_MB} MiB target={TARGET_MB} MiB", flush=True)
        time.sleep(EVERY_S)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=load, daemon=True).start()
    ThreadingHTTPServer(("", 8080), Handler).serve_forever()
