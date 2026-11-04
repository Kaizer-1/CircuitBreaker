#!/usr/bin/env bash
# Screenshot every mockup with headless Chrome: dashboards at 1920x1080 (plus a 25% copy, the
# back-of-the-room test), phones at 390x844 (2x). Usage: docs/design/mockups/render.sh <output dir>
set -euo pipefail
out="${1:?usage: render.sh <output dir>}"
here="$(cd "$(dirname "$0")" && pwd)"
chrome="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
mkdir -p "$out"
for d in pink-paper dalal-street terminal; do
  "$chrome" --headless=new --disable-gpu --hide-scrollbars --window-size=1920,1080 --virtual-time-budget=8000 \
    --screenshot="$out/$d-dashboard-1920x1080.png" "file://$here/$d/dashboard.html" 2>/dev/null
  "$chrome" --headless=new --disable-gpu --hide-scrollbars --window-size=390,844 --force-device-scale-factor=2 \
    --virtual-time-budget=8000 --screenshot="$out/$d-mobile-390x844.png" "file://$here/$d/mobile.html" 2>/dev/null
  sips -Z 480 "$out/$d-dashboard-1920x1080.png" --out "$out/$d-dashboard-25pct.png" >/dev/null
done
ls "$out"
