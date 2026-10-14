#!/bin/sh
# Rebuild the README tour from the checked-in synthetic demo screenshots.
set -eu

ASSET_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
FFMPEG_BIN=${FFMPEG:-ffmpeg}

"$FFMPEG_BIN" -hide_banner -loglevel error -y \
  -loop 1 -framerate 15 -t 6 -i "$ASSET_DIR/dashboard.png" \
  -loop 1 -framerate 15 -t 6 -i "$ASSET_DIR/backups.png" \
  -loop 1 -framerate 15 -t 6 -i "$ASSET_DIR/restore.png" \
  -filter_complex \
  '[0:v]scale=1280:736:force_original_aspect_ratio=decrease,pad=1280:736:(ow-iw)/2:(oh-ih)/2:color=0x0b1020,setsar=1,fade=t=in:st=0:d=0.4,fade=t=out:st=5.6:d=0.4[v0];[1:v]scale=1280:736:force_original_aspect_ratio=decrease,pad=1280:736:(ow-iw)/2:(oh-ih)/2:color=0x0b1020,setsar=1,fade=t=in:st=0:d=0.4,fade=t=out:st=5.6:d=0.4[v1];[2:v]scale=1280:736:force_original_aspect_ratio=decrease,pad=1280:736:(ow-iw)/2:(oh-ih)/2:color=0x0b1020,setsar=1,fade=t=in:st=0:d=0.4,fade=t=out:st=5.6:d=0.4[v2];[v0][v1][v2]concat=n=3:v=1:a=0[out]' \
  -map '[out]' -c:v libx264 -preset medium -crf 23 -pix_fmt yuv420p \
  -movflags +faststart -an "$ASSET_DIR/product-tour.mp4"

"$FFMPEG_BIN" -hide_banner -loglevel error -y \
  -i "$ASSET_DIR/product-tour.mp4" \
  -filter_complex \
  'fps=4,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4' \
  -loop 0 "$ASSET_DIR/product-tour.gif"
