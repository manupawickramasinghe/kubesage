"""Voice-over for the demo video: one WAV per scene of narration.json, read by Piper (local, offline TTS), and
durations.json, which record.py uses to hold each scene for its narration."""
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).parent
WORK = Path(os.environ.get("KUBESAGE_VIDEO_WORK", HERE / "build"))
VOICE = os.environ.get("PIPER_VOICE", str(WORK / "voices" / "en_US-lessac-high.onnx"))

(WORK / "audio").mkdir(parents=True, exist_ok=True)
out = []
for s in json.loads((HERE / "narration.json").read_text()):
    wav = WORK / "audio" / f"{s['id']}.wav"
    subprocess.run([sys.executable, "-m", "piper", "-m", VOICE, "-f", str(wav), "--length-scale", "1.06",
                    "--sentence-silence", "0.35"], input=s["narration"], text=True, check=True, capture_output=True)
    with wave.open(str(wav)) as w:
        out.append({"id": s["id"], "seconds": round(w.getnframes() / w.getframerate(), 2), "words": len(s["narration"].split())})
(WORK / "audio" / "durations.json").write_text(json.dumps(out, indent=1))
print(f"{len(out)} clips, {sum(x['seconds'] for x in out):.1f} s of narration")
