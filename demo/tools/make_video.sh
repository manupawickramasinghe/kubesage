#!/usr/bin/env bash
# Rebuild demo/video/kubesage-demo.mp4: voice-over, a recording of the slides and the dashboard against the replay
# server, then ffmpeg. Needs Python with Playwright and Chromium (the repo's .venv plus `playwright install chromium`), ffmpeg, and
# Kokoro for the voice (installed into its own venv below, with its ~340 MB model). Nothing here touches a cluster or
# calls a model API.
set -euo pipefail
cd "$(dirname "$0")"
WORK=${KUBESAGE_VIDEO_WORK:-$PWD/build}
OUT=${1:-../video/kubesage-demo.mp4}
PY=${PYTHON:-../../.venv/bin/python}
export KUBESAGE_VIDEO_WORK=$WORK
mkdir -p "$WORK/kokoro"
if [ ! -x "$WORK/tts/bin/python" ]; then python3 -m venv "$WORK/tts" && "$WORK/tts/bin/pip" install -q kokoro-onnx soundfile; fi
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
  [ -f "$WORK/kokoro/$f" ] || curl -fL -o "$WORK/kokoro/$f" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
done
"$WORK/tts/bin/python" tts.py
"$PY" record.py          # under three minutes, in real time
"$PY" build.py "$OUT"
