#!/usr/bin/env bash
# Joins the two recordings into the one film the README and the docs site play:
# the console first, then the session with Claude. Nothing is added between
# them; each half is the film encode.sh made, as it was recorded.
#
#   DEST=demo-out scripts/demo/encode.sh console console-demo intro:end
#   DEST=demo-out scripts/demo/encode.sh claude claude-demo ask:answer+5.5 audit+2:end
#   scripts/demo/join.sh
#
# Reads $SRC/console-demo.{mp4,gif} and $SRC/claude-demo.{mp4,gif}; writes
# $DEST/truejoin-demo.mp4 (both whole) and $DEST/truejoin-demo.gif (each GIF,
# scaled to one width with one palette). WIDTH and COLORS tune the GIF's size,
# which tests/unified/test_packaging.py holds under 4 MB.
set -euo pipefail
SRC=${SRC:-demo-out}
DEST=${DEST:-docs/docs/assets}
for part in console-demo claude-demo; do
  for kind in mp4 gif; do
    [ -f "$SRC/$part.$kind" ] || { echo "missing $SRC/$part.$kind (encode.sh with DEST=$SRC writes it)" >&2; exit 1; }
  done
done
mkdir -p "$DEST"

ffmpeg -y -loglevel error -i "$SRC/console-demo.mp4" -i "$SRC/claude-demo.mp4" \
  -filter_complex "[0:v]setsar=1[a];[1:v]setsar=1[b];[a][b]concat=n=2:v=1:a=0,format=yuv420p" \
  -c:v libx264 -crf "${CRF:-23}" -preset slow -movflags +faststart -an "$DEST/truejoin-demo.mp4"

W=${WIDTH:-720}
ffmpeg -y -loglevel error -i "$SRC/console-demo.gif" -i "$SRC/claude-demo.gif" -filter_complex \
  "[0:v]scale=$W:-1:flags=lanczos,setsar=1[a];[1:v]scale=$W:-1:flags=lanczos,setsar=1[b];[a][b]concat=n=2:v=1:a=0,split[x][y];[x]palettegen=max_colors=${COLORS:-64}:stats_mode=diff[p];[y][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  "$DEST/truejoin-demo.gif"

claude_at=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$SRC/console-demo.mp4")
ls -l "$DEST/truejoin-demo.mp4" "$DEST/truejoin-demo.gif" | awk '{printf "%8.2f MB  %s\n", $5 / 1048576, $9}'
echo "Claude's half starts at ${claude_at%.*} s"
