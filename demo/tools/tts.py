"""Voice-over for the demo video: one WAV per scene of narration.json, read by Kokoro (an open text-to-speech model
that runs locally: no key, nothing sent anywhere), and durations.json, which record.py uses to hold each scene for
its narration. SAY respells a few names for the voice; the subtitles keep the written form."""
import json
import os
import re
import wave
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

HERE = Path(__file__).parent
WORK = Path(os.environ.get("KUBESAGE_VIDEO_WORK", HERE / "build"))
MODEL = Path(os.environ.get("KOKORO_DIR", WORK / "kokoro"))
VOICE, SPEED = os.environ.get("KOKORO_VOICE", "af_heart"), float(os.environ.get("KOKORO_SPEED", "1.0"))
SAY = {"ContainerOOMKilled": "Container O-O-M Killed", "KEDA": "Keda", "SRE": "S-R-E", "VM": "V-M"}

kokoro = Kokoro(str(MODEL / "kokoro-v1.0.onnx"), str(MODEL / "voices-v1.0.bin"))
(WORK / "audio").mkdir(parents=True, exist_ok=True)
out = []
for s in json.loads((HERE / "narration.json").read_text()):
    text = re.sub("|".join(rf"\b{re.escape(k)}\b" for k in SAY), lambda m: SAY[m.group(0)], s["narration"])
    samples, rate = kokoro.create(text, voice=VOICE, speed=SPEED, lang="en-us")
    pcm = (np.clip(samples, -1, 1) * 32767 * 0.9).astype("<i2")  # a little headroom below full scale
    with wave.open(str(WORK / "audio" / f"{s['id']}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    out.append({"id": s["id"], "seconds": round(len(pcm) / rate, 2), "words": len(s["narration"].split())})
(WORK / "audio" / "durations.json").write_text(json.dumps(out, indent=1))
print(f"{len(out)} clips, {sum(x['seconds'] for x in out):.1f} s of narration, voice {VOICE}")
