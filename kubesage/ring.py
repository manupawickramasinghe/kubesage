"""A fixed-size ring buffer of text lines in RAM (/dev/shm), shared by every process that writes to it.

Everything KubeSage shows as terminal output goes through here: the agents' streams, the journal, test-case pod
logs and tmux panes piped with `kubesage ring write`. The web UI and `kubesage ring tail` only read it, so nothing
on the web can write to a terminal. When the ring is full the oldest segment is overwritten.

Layout: a 4 KiB header, then `nseg` segments of `segsize` bytes. A record never spans segments.
  header   magic(8) segsize(u64) nseg(u64) cur_seg(u64, ever-increasing; slot = cur_seg % nseg) next_rec(u64)
  segment  seg_seq(u64) used(u32) pad(u32) first_rec(u64), then records
  record   length(u32) rec_seq(u64) ts(f64) stream_len(u8) stream payload(utf-8)
Writers append under flock. Readers take no lock: `used` is published after the record bytes, and a reader
re-checks `seg_seq` after copying, so a segment overwritten mid-read is dropped rather than returned torn.
"""
from __future__ import annotations

import fcntl
import mmap
import os
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"KSRING01"
HEADER = struct.Struct("<8sQQQQ")
SEG = struct.Struct("<QII Q".replace(" ", ""))
REC = struct.Struct("<IQdB")
HEADER_SIZE = 4096
MAX_PAYLOAD = 64 * 1024
DEFAULT_PATH = "/dev/shm/kubesage-ring"
DEFAULT_MB = 500


def default_path() -> Path:
    return Path(os.environ.get("KUBESAGE_RING", DEFAULT_PATH))


def default_mb() -> int:
    return int(os.environ.get("KUBESAGE_RING_MB", DEFAULT_MB))


@dataclass(frozen=True)
class Record:
    seq: int
    ts: float
    stream: str
    text: str

    def as_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "stream": self.stream, "text": self.text}


class Ring:
    def __init__(self, path: str | Path | None = None, size_mb: int | None = None, *, segsize: int = 1 << 20,
                 create: bool = True):
        self.path = Path(path or default_path())
        if not self.path.exists():
            if not create:
                raise FileNotFoundError(f"no ring at {self.path}; start `kubesage serve` or `kubesage ring init`")
            nseg = max(4, (size_mb or default_mb()) * (1 << 20) // segsize)
            self._create(nseg, segsize)
        self._fd = os.open(self.path, os.O_RDWR)
        size = os.fstat(self._fd).st_size
        self._mm = mmap.mmap(self._fd, size)
        magic, self.segsize, self.nseg, _, _ = HEADER.unpack_from(self._mm, 0)
        if magic != MAGIC or HEADER_SIZE + self.segsize * self.nseg != size:
            raise ValueError(f"{self.path} is not a KubeSage ring")
        self._local = threading.Lock()  # flock is per open file, so threads of one process serialise here first

    def _create(self, nseg: int, segsize: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}")
        with open(tmp, "wb") as f:
            f.truncate(HEADER_SIZE + nseg * segsize)  # sparse: RAM is only used as segments fill
            f.write(HEADER.pack(MAGIC, segsize, nseg, 0, 1))
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, self.path)  # atomic, and loses gracefully to a concurrent creator
        except FileExistsError:
            pass
        finally:
            tmp.unlink()

    # ---- writing ----
    def write(self, stream: str, text: str, ts: float | None = None) -> int:
        stream_b = stream.encode()[:255]
        payload = text.encode("utf-8", "replace")[:MAX_PAYLOAD]
        length = REC.size + len(stream_b) + len(payload)
        with self._local:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            try:
                _, _, _, cur, rec_seq = HEADER.unpack_from(self._mm, 0)
                base = self._seg_offset(cur)
                seg_seq, used, _, first = SEG.unpack_from(self._mm, base)
                if seg_seq != cur or cur == 0:  # a fresh ring: segment 0 was never initialised
                    cur = max(cur, 1)
                    base = self._seg_offset(cur)
                    self._start_segment(base, cur, rec_seq)
                    used = 0
                elif SEG.size + used + length > self.segsize:
                    cur += 1
                    base = self._seg_offset(cur)
                    self._start_segment(base, cur, rec_seq)
                    used = 0
                at = base + SEG.size + used
                REC.pack_into(self._mm, at, length, rec_seq, ts or time.time(), len(stream_b))
                self._mm[at + REC.size:at + REC.size + len(stream_b)] = stream_b
                self._mm[at + REC.size + len(stream_b):at + length] = payload
                struct.pack_into("<I", self._mm, base + 8, used + length)  # publish the record
                struct.pack_into("<QQ", self._mm, 24, cur, rec_seq + 1)
                return rec_seq
            finally:
                fcntl.flock(self._fd, fcntl.LOCK_UN)

    def write_lines(self, stream: str, text: str) -> None:
        for line in text.splitlines():
            self.write(stream, line)

    def _seg_offset(self, seg: int) -> int:
        return HEADER_SIZE + (seg % self.nseg) * self.segsize

    def _start_segment(self, base: int, seg: int, first_rec: int) -> None:
        struct.pack_into("<Q", self._mm, base, 0)  # invalidate first, so readers never trust a half-reset segment
        SEG.pack_into(self._mm, base, 0, 0, 0, first_rec)
        struct.pack_into("<Q", self._mm, base, seg)

    # ---- reading ----
    def _state(self) -> tuple[int, int]:
        _, _, _, cur, next_rec = HEADER.unpack_from(self._mm, 0)
        return cur, next_rec

    def _segments(self) -> list[tuple[int, int]]:
        """(seg_seq, first_rec) of every live segment, oldest first."""
        cur, _ = self._state()
        out = []
        for seg in range(max(1, cur - self.nseg + 1), cur + 1):
            seg_seq, _, _, first = SEG.unpack_from(self._mm, self._seg_offset(seg))
            if seg_seq == seg:
                out.append((seg, first))
        return out

    def _records(self, seg: int) -> list[Record]:
        base = self._seg_offset(seg)
        seg_seq, used, _, _ = SEG.unpack_from(self._mm, base)
        if seg_seq != seg:
            return []
        data = self._mm[base + SEG.size:base + SEG.size + min(used, self.segsize - SEG.size)]
        if struct.unpack_from("<Q", self._mm, base)[0] != seg:
            return []  # overwritten while copying
        out, at = [], 0
        while at + REC.size <= len(data):
            length, seq, ts, slen = REC.unpack_from(data, at)
            if length < REC.size + slen or at + length > len(data):
                break
            stream = data[at + REC.size:at + REC.size + slen].decode("utf-8", "replace")
            out.append(Record(seq, ts, stream, data[at + REC.size + slen:at + length].decode("utf-8", "replace")))
            at += length
        return out

    def read(self, after: int = 0, *, limit: int = 1000, streams: list[str] | None = None) -> tuple[list[Record], int]:
        """Records with seq > after (the oldest still held, if `after` was overwritten), and the newest seq."""
        segs = self._segments()
        _, next_rec = self._state()
        start = 0
        for i, (_, first) in enumerate(segs):
            if first <= after + 1:
                start = i
        out: list[Record] = []
        for seg, _ in segs[start:]:
            for rec in self._records(seg):
                if rec.seq > after and _matches(rec.stream, streams):
                    out.append(rec)
                    if len(out) >= limit:
                        return out, next_rec - 1
        return out, next_rec - 1

    def tail(self, n: int = 200, *, streams: list[str] | None = None) -> tuple[list[Record], int]:
        """The newest `n` matching records, scanning back only as far as needed."""
        segs = self._segments()
        _, next_rec = self._state()
        out: list[Record] = []
        for seg, _ in reversed(segs):
            recs = [r for r in self._records(seg) if _matches(r.stream, streams)]
            out[:0] = recs
            if len(out) >= n:
                break
        return out[-n:], next_rec - 1

    def iter_since(self, since_ts: float = 0, *, streams: list[str] | None = None) -> Iterator[Record]:
        for seg, _ in self._segments():
            for rec in self._records(seg):
                if rec.ts >= since_ts and _matches(rec.stream, streams):
                    yield rec

    def stat(self) -> dict:
        segs = self._segments()
        cur, next_rec = self._state()
        used = sum(SEG.size + SEG.unpack_from(self._mm, self._seg_offset(s))[1] for s, _ in segs)
        oldest = next((r.ts for s, _ in segs for r in self._records(s)[:1]), None)
        newest_recs = self._records(segs[-1][0]) if segs else []
        streams: dict[str, int] = {}
        for rec in newest_recs[-2000:]:
            streams[rec.stream] = streams.get(rec.stream, 0) + 1
        return {"path": str(self.path), "capacity_bytes": self.segsize * self.nseg, "used_bytes": used,
                "segments": self.nseg, "segments_used": len(segs), "wrapped": cur > self.nseg,
                "records": next_rec - 1 - (segs[0][1] - 1 if segs else 0), "last_seq": next_rec - 1,
                "oldest_ts": oldest, "newest_ts": newest_recs[-1].ts if newest_recs else None,
                "recent_streams": sorted(streams)}

    def close(self) -> None:
        self._mm.close()
        os.close(self._fd)


def _matches(stream: str, prefixes: list[str] | None) -> bool:
    return not prefixes or any(stream == p or stream.startswith(p.rstrip("*")) for p in prefixes)


def format_line(rec: Record, *, show_stream: bool = True) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(rec.ts)) + f".{int(rec.ts % 1 * 1000):03d}"
    return f"{stamp} {rec.stream:<18} {rec.text}" if show_stream else f"{stamp} {rec.text}"


_shared: Ring | None = None
_shared_lock = threading.Lock()


def shared() -> Ring | None:
    """The process-wide ring, opened on first use; None if /dev/shm isn't writable (output then only goes to the
    in-process journal)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            try:
                _shared = Ring()
            except (OSError, ValueError):
                return None
        return _shared


def emit(stream: str, text: str) -> None:
    ring = shared()
    if ring is not None:
        for line in str(text).splitlines() or [""]:
            ring.write(stream, line)
