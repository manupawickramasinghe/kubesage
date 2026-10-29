#!/usr/bin/env bash
# Rebuild demo/video/kubesage-demo.mp4: voice-over, a recording of the dashboard against the replay server, then
# ffmpeg. Needs Python with Playwright and Chromium (the repo's .venv plus `playwright install chromium`), ffmpeg,
# and Piper for the voice (installed into its own venv below). Nothing here touches a cluster or calls a model.
set -euo pipefail
cd "$(dirname "$0")"
WORK=${KUBESAGE_VIDEO_WORK:-$PWD/build}
OUT=${1:-../video/kubesage-demo.mp4}
PY=${PYTHON:-../../.venv/bin/python}
export KUBESAGE_VIDEO_WORK=$WORK
mkdir -p "$WORK/voices"
if [ ! -x "$WORK/tts/bin/python" ]; then python3 -m venv "$WORK/tts" && "$WORK/tts/bin/pip" install -q piper-tts; fi
[ -f "$WORK/voices/en_US-lessac-high.onnx" ] || (cd "$WORK/voices" && "$WORK/tts/bin/python" -m piper.download_voices en_US-lessac-high)
"$WORK/tts/bin/python" tts.py
"$PY" record.py          # about four and a half minutes, in real time
"$PY" build.py "$OUT"
