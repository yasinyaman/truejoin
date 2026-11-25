#!/usr/bin/env bash
# Turns a recording (demo-out/<name>.webm with its scenes.json) into the two
# files the README shows: the whole of it as an MP4 any browser plays, and a
# short silent preview as a GIF, cut from the scenes named.
#
#   scripts/demo/encode.sh console console-demo tenant:question admin+7:catalog audit:guide
#
# A cut is from:to, each a scene of scenes.json, with an optional +seconds.
set -euo pipefail
OUT=${OUT:-demo-out}
DEST=${DEST:-docs/docs/assets}
name=$1 target=$2
shift 2
scenes=$OUT/$name.scenes.json
[ -f "$scenes" ] || scenes=$OUT/scenes.json
mkdir -p "$DEST"

at() { # the second a scene starts at, plus what follows a "+"
  python3 - "$scenes" "$1" <<'PY'
import json, sys
scene, _, more = sys.argv[2].partition("+")
start = next(s["at"] for s in json.load(open(sys.argv[1])) if s["name"] == scene)
print(round(start + float(more or 0), 2))
PY
}

# The page takes a moment to draw: the film starts where the first scene does.
start=$(python3 -c "import json; print(json.load(open('$scenes'))[0]['at'])")
ffmpeg -y -loglevel error -ss "$start" -i "$OUT/$name.webm" -vf "fps=25,format=yuv420p" \
  -c:v libx264 -crf "${CRF:-25}" -preset slow -movflags +faststart -an "$DEST/$target.mp4"

picked=""
for cut in "$@"; do
  from=$(at "${cut%%:*}") to=$(at "${cut##*:}")
  picked="${picked:+$picked+}between(t,$from,$to)"
done
ffmpeg -y -loglevel error -i "$OUT/$name.webm" -filter_complex \
  "fps=25,select='$picked',setpts=N/25/TB,fps=${FPS:-8},scale=${WIDTH:-800}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=${COLORS:-96}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  "$DEST/$target.gif"
ls -l "$DEST/$target.mp4" "$DEST/$target.gif" | awk '{printf "%8.2f MB  %s\n", $5 / 1048576, $9}'
