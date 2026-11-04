"""Background music for the demo video, composed here from scratch: no samples, no downloads, no licence to check.

An upbeat 124 BPM track (I-V-vi-IV in F major: kick, clap, hats, an off-beat bass, a pumping pad and a delayed
arpeggio) laid out on the recorded timeline (scenes.json): a filtered build under the cold open, the drop on the title's
reveal ("This is KubeSage"), a whoosh at every slide change, a lift into the website part and a final chord on the
close. build.py ducks it under the voice. Writes WORK/music.wav (44.1 kHz stereo). Needs only numpy.
"""
import json
import os
import re
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
WORK = Path(os.environ.get("KUBESAGE_VIDEO_WORK", HERE / "build"))
SR = 44100
BPM = 124
BEAT = 60 / BPM
BAR = 4 * BEAT
LEAD = 0.4  # the voice starts this long after its scene (record.py)
rng = np.random.default_rng(7)

# I-V-vi-IV in F: (pad notes, bass root, arpeggio notes) as MIDI numbers
CHORDS = [([60, 65, 69], 41, [65, 69, 72, 77]),   # F
          ([60, 64, 67], 36, [64, 67, 72, 76]),   # C
          ([62, 65, 69], 38, [62, 65, 69, 74]),   # Dm
          ([58, 62, 65], 34, [62, 65, 70, 74])]   # Bb


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def tt(seconds):
    return np.arange(int(seconds * SR)) / SR


def shape(x, sr=SR, lo=None, hi=None, order=2):
    """Zero-phase FFT filter: high-pass at lo Hz, low-pass at hi Hz (Butterworth-like magnitude)."""
    n = len(x)
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(n, 1 / sr)
    g = np.ones_like(f)
    if hi:
        g /= np.sqrt(1 + (f / hi) ** (2 * order))
    if lo:
        g /= np.sqrt(1 + (lo / np.maximum(f, 1e-3)) ** (2 * order))
    return np.fft.irfft(X * (g[:, None] if X.ndim == 2 else g), n=n, axis=0)


def saw(freq, t, top=6000, detune=0.0):
    out = np.zeros_like(t)
    f = freq * 2 ** (detune / 12)
    for k in range(1, max(2, int(top / f)) + 1):
        out += ((-1) ** (k + 1)) * np.sin(2 * np.pi * k * f * t + k * 0.3) / k
    return out * (2 / np.pi)


def adsr(n, a, d, s, r, sustain_len):
    """Attack/decay/sustain/release envelope in seconds, sustain level s, total n samples."""
    t = np.arange(n) / SR
    env = np.where(t < a, t / max(a, 1e-4), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-4)))
    rel = t > sustain_len
    env[rel] *= np.exp(-(t[rel] - sustain_len) / max(r, 1e-4))
    return env


# ---------------------------------------------------------------- the sounds
def kick():
    t = tt(0.45)
    f = 46 + 110 * np.exp(-t / 0.04)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.17)
    click = shape(rng.standard_normal(len(t)), lo=2500) * np.exp(-t / 0.003) * 0.3
    return np.tanh(1.6 * (body + click)) * 0.95


def clap():
    t = tt(0.32)
    env = sum(np.exp(-np.maximum(t - o, 0) / 0.006) * (t >= o) for o in (0, .008, .017)) * 0.6 + np.exp(-t / 0.11) * 0.5
    return shape(rng.standard_normal(len(t)), lo=900, hi=6000) * env * 0.55


def hat(open_=False):
    t = tt(0.3 if open_ else 0.06)
    return shape(rng.standard_normal(len(t)), lo=7000 if not open_ else 6000) * np.exp(-t / (0.07 if open_ else 0.014)) * 0.55


def crash():
    t = tt(2.4)
    return shape(rng.standard_normal((len(t), 2)), lo=3500, hi=14000) * np.exp(-t / 0.55)[:, None] * 0.35


def whoosh():
    t = tt(0.55)
    n = len(t)
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    hop, win = 512, np.hanning(2048)
    for i in range(0, n - 2048, hop):  # a band that sweeps up through the noise
        c = 400 + 5000 * (i / n) ** 1.5
        seg = shape(noise[i:i + 2048] * win, lo=c * 0.6, hi=c * 1.6)
        out[i:i + 2048] += seg
    env = np.sin(np.pi * np.clip(t / 0.55, 0, 1)) ** 2
    return out * env * 0.25


def bass_note(m, length):
    t = tt(length + 0.05)
    x = saw(hz(m), t, top=1400) * 0.7 + np.sin(2 * np.pi * hz(m) * t) * 0.6
    return shape(x, hi=700) * adsr(len(t), .004, .09, .55, .03, length) * 0.42


def pad_chord(notes, length, cutoff):
    t = tt(length + 0.5)
    x = sum(saw(hz(m), t, top=4500, detune=d) for m in notes for d in (-.11, 0, .11))
    return shape(x, hi=cutoff) * adsr(len(t), .18, .6, .8, .35, length)[:] * 0.085


def pluck(m):
    t = tt(0.32)
    x = saw(hz(m), t, top=7000) * 0.6 + np.sin(2 * np.pi * hz(m) * 2 * t) * 0.15
    return shape(x, hi=6000) * np.exp(-t / 0.085) * 0.16


def riser(length):
    t = tt(length)
    n = len(t)
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    hop, win = 1024, np.hanning(4096)
    for i in range(0, n - 4096, hop):
        c = 300 + 7000 * (i / n) ** 2
        out[i:i + 4096] += shape(noise[i:i + 4096] * win, lo=c * 0.5, hi=c * 1.5)
    sweep = np.sin(2 * np.pi * np.cumsum(220 + 900 * (t / length) ** 2) / SR) * 0.06
    return (out * 0.12 + sweep) * (t / length) ** 2


# ---------------------------------------------------------------- the timeline
def sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", text) if x]


def main():
    rec = json.loads((WORK / "scenes.json").read_text())
    durs = {d["id"]: d["seconds"] for d in json.loads((WORK / "audio" / "durations.json").read_text())}
    say = {s["id"]: s["narration"] for s in json.loads((HERE / "narration.json").read_text())}
    start, total = rec["start"], rec["stop"] - rec["start"]
    at = {s["id"]: s["t"] - start for s in rec["scenes"]}
    parts = sentences(say["title"])  # the drop lands on the title's last sentence: "This is KubeSage..."
    share = sum(len(x) for x in parts[:-1]) / sum(len(x) for x in parts)
    drop = at["title"] + LEAD + durs["title"] * share
    web, close = at["d-analyze"], at["close"]
    end_hit = drop + np.floor((total - 2.8 - drop) / BAR) * BAR  # the last downbeat that leaves room for the tail

    n = int((total + 3) * SR)
    L = {k: np.zeros((n, 2)) for k in ("drums", "bass", "pad", "arp", "fx", "send")}

    def put(layer, x, t, pan=0.0, gain=1.0):
        i = int(round(t * SR))
        if i >= n or i + len(x) <= 0:
            return
        x = x if x.ndim == 2 else np.stack([x * (1 - max(pan, 0)), x * (1 + min(pan, 0))], axis=1)
        lo = max(0, -i)
        hi = min(len(x), n - i)
        L[layer][i + lo:i + hi] += x[lo:hi] * gain

    K, C, H, HO, CR, W = kick(), clap(), hat(), hat(True), crash(), whoosh()
    bass_cache, pluck_cache, pad_cache = {}, {}, {}
    first_bar = -int(np.ceil(drop / BAR))
    last_bar = int((end_hit - drop) / BAR)
    for b in range(first_bar, last_bar):
        t0 = drop + b * BAR
        notes, root, arp = CHORDS[b % 4]
        intro = b < 0
        groove = not intro
        lift = t0 >= web - 1e-6
        # pad: opens up through the build, full in the groove
        cutoff = 800 + 1700 * (1 - (-b) / max(1, -first_bar)) if intro else 2600
        key = (tuple(notes), int(cutoff))
        put("pad", pad_cache.setdefault(key, pad_chord(notes, BAR, cutoff) if key not in pad_cache else None), t0)
        for s in range(16):  # sixteenth steps
            ts = t0 + s * BEAT / 4
            if ts < 0:
                continue
            beat, sub = divmod(s, 4)
            if groove:
                if sub == 0:
                    put("drums", K, ts, gain=0.9)
                if sub == 0 and beat in (1, 3):
                    put("drums", C, ts, pan=0.05)
                if sub == 2:
                    put("drums", HO if lift and beat % 2 else H, ts, pan=0.25, gain=1.0)
                elif sub in (1, 3):
                    put("drums", H, ts, pan=-0.25, gain=0.45)
            elif b >= -3:  # the build: hats and a soft kick, then a snare roll into the drop
                if sub == 2:
                    put("drums", H, ts, pan=0.25, gain=0.8)
                if sub == 0 and b >= -2:
                    put("drums", K, ts, gain=0.45)
                if b == -1 and beat >= 2:
                    put("drums", C, ts, gain=0.25 + 0.5 * (s - 8) / 8)
            if sub == 2 and (groove or b >= -3):  # off-beat bass
                m = root + (12 if beat == 3 else 0)
                note = bass_cache.setdefault(m, bass_note(m, BEAT / 2 * 0.9))
                put("bass", note, ts, gain=1.0 if groove else 0.85)
            if groove:  # the arpeggio, up an octave in the website part
                m = arp[[0, 1, 2, 3, 2, 1, 2, 3][s % 8]] + (12 if lift and s % 4 == 3 else 0)
                p = pluck_cache.setdefault(m, pluck(m))
                put("arp", p, ts, pan=0.3 * np.sin(s * 1.3))
    # moments: the build, the drop, the move to the website, the slide changes, the end
    rs = riser(min(2 * BAR, drop))
    put("fx", rs, drop - len(rs) / SR, gain=1.0)
    put("fx", CR, drop, gain=1.0)
    put("fx", CR, web, gain=0.7)
    put("fx", W, web - 0.3, gain=1.2)
    for sid in ("summary", "server", "rush", "finops", "team", "close"):
        if sid in at:
            put("fx", W, at[sid] - 0.2, gain=1.0)
    notes, root, arp = CHORDS[0]
    put("drums", K, end_hit, gain=1.0)
    put("fx", CR, end_hit, gain=1.0)
    put("pad", pad_chord(notes + [72], 2.6, 2600), end_hit, gain=1.3)
    put("bass", bass_note(root, 1.6), end_hit, gain=1.2)
    for i, m in enumerate(arp + [arp[0] + 12]):
        put("arp", pluck(m), end_hit + i * 0.06, pan=0.4 * np.sin(i), gain=1.1)

    # pumping: pad and bass duck after every kick in the groove
    t = np.arange(n) / SR
    since = np.where(t >= drop, (t - drop) % BEAT, 9.0)
    since[t >= end_hit] = 9.0
    pump = (1 - 0.55 * np.exp(-since / 0.09))[:, None]
    L["pad"] *= pump
    L["bass"] *= (1 - 0.35 * np.exp(-since / 0.07))[:, None]
    # a ping-pong delay on the arpeggio, then one small room for everything that wants air
    d = int(0.75 * BEAT * SR)
    arp = L["arp"].copy()
    for k, g in enumerate((0.38, 0.22, 0.12), 1):
        echo = np.zeros_like(arp)
        echo[k * d:] = arp[:-k * d]
        L["arp"][:, k % 2] += echo[:, k % 2] * g * 1.6
    send = L["pad"] * 0.5 + L["arp"] * 0.6 + L["drums"] * 0.08 + L["fx"] * 0.4
    it = tt(1.8)
    ir = rng.standard_normal((len(it), 2)) * np.exp(-it / 0.5)[:, None]
    ir = shape(ir, hi=6000)
    ir[: int(0.015 * SR)] = 0
    ir /= np.sqrt((ir ** 2).sum(axis=0))
    m = 1 << int(np.ceil(np.log2(n + len(ir))))
    wet = np.stack([np.fft.irfft(np.fft.rfft(send[:, c], m) * np.fft.rfft(ir[:, c], m), m)[:n] for c in range(2)], axis=1)
    mix = L["drums"] * 1.0 + L["bass"] * 1.0 + L["pad"] * 1.0 + L["arp"] * 0.9 + L["fx"] * 0.9 + wet * 0.28
    mix = shape(mix, lo=32)
    fade = np.clip((total - t) / 2.2, 0, 1)[:, None] ** 1.5  # the chord's tail fades out with the video
    fade[t < end_hit] = 1
    mix *= fade
    mix = np.tanh(mix * 1.1) / np.tanh(1.1)
    mix *= 0.89 / max(1e-9, np.abs(mix).max())
    pcm = (mix[: int(total * SR)] * 32767).astype("<i2")
    with wave.open(str(WORK / "music.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"music.wav {total:.1f} s, drop at {drop:.1f} s, website lift at {web:.1f} s, last chord at {end_hit:.1f} s")


if __name__ == "__main__":
    main()
