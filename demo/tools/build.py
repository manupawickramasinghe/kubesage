"""Assemble the recorded frames, the voice-over and subtitles into demo/video/kubesage-demo.mp4."""
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).parent
WORK = Path(os.environ.get("KUBESAGE_VIDEO_WORK", HERE / "build"))  # frames, audio and timings
OUT = Path(sys.argv[1])
LEAD = 0.4  # the voice starts this long after its scene

frames = json.loads((WORK / "frames.json").read_text())
rec = json.loads((WORK / "scenes.json").read_text())
narr = {s["id"]: s["narration"] for s in json.loads((HERE / "narration.json").read_text())}
start, stop = rec["start"], rec["stop"]
total = stop - start

# Video: each frame lasts until the next one (the screencast only sends a frame when the page repaints).
fr = frames["frames"]
first = max((i for i, (t, _) in enumerate(fr) if t <= start), default=0)
fr = fr[first:]
lines = []
for i, (t, name) in enumerate(fr):
    t0 = max(0.0, t - start)
    t1 = (fr[i + 1][0] - start) if i + 1 < len(fr) else total
    if t1 - t0 <= 0:
        continue
    lines += [f"file '{WORK / 'frames' / name}'", f"duration {t1 - t0:.4f}"]
lines.append(f"file '{WORK / 'frames' / fr[-1][1]}'")
(WORK / "concat.txt").write_text("\n".join(lines) + "\n")

# Audio: one track, each scene's clip at its scene's start.
rate = 22050
track = bytearray(int((total + 1) * rate) * 2)
for s in rec["scenes"]:
    clip = WORK / "audio" / f"{s['id']}.wav"
    if not clip.exists():
        continue
    with wave.open(str(clip)) as w:
        data = w.readframes(w.getnframes())
    at = int((s["t"] - start + LEAD) * rate) * 2
    track[at:at + len(data)] = data
with wave.open(str(WORK / "voice.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(bytes(track))

# Subtitles: each clip's sentences, timed by their share of its characters.
durs = {d["id"]: d["seconds"] for d in json.loads((WORK / "audio" / "durations.json").read_text())}
stamp = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x * 1000 % 1000):03d}"
cues = []
for s in rec["scenes"]:
    if s["id"] not in narr:
        continue
    sentences = re.split(r"(?<=[.!?:])\s+", narr[s["id"]])
    chars = sum(len(x) for x in sentences)
    t = s["t"] - start + LEAD
    for x in sentences:
        d = durs[s["id"]] * len(x) / chars
        cues.append((t, t + d, x))
        t += d
srt = "\n".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1))
OUT.parent.mkdir(parents=True, exist_ok=True)
(OUT.with_suffix(".srt")).write_text(srt)

subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(WORK / "concat.txt"),
                "-i", str(WORK / "voice.wav"), "-i", str(OUT.with_suffix(".srt")),
                "-map", "0:v", "-map", "1:a", "-map", "2:s",
                "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
                "-metadata", "title=KubeSage: a replay of the demo on a two-machine cluster",
                "-movflags", "+faststart", "-t", f"{total:.2f}", str(OUT)], check=True)
print(OUT, f"{total:.1f} s, {len(cues)} subtitle cues")
