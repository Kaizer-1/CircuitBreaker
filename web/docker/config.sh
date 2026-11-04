#!/bin/sh
# Runs when the web container starts (nginx's /docker-entrypoint.d/): writes /config.js with
# PUBLIC_BASE_URL, the address phones use to reach this app. The join QR code encodes
# PUBLIC_BASE_URL/join. On demo day set it to the laptop's LAN address on the phone hotspot,
# e.g. PUBLIC_BASE_URL=http://192.168.43.12:5173 make up (docs/08-developer-setup.md).
set -eu
url="${PUBLIC_BASE_URL:-}"
case "$url" in
  *'"'* | *'\'* | *'<'* | *'>'* | *' '*)
    echo "config.sh: PUBLIC_BASE_URL contains a character that is not allowed; ignoring it" >&2
    url=""
    ;;
esac
printf 'window.CB_CONFIG = { publicBaseUrl: "%s" }\n' "$url" > /tmp/config.js
echo "config.sh: join QR code points at ${url:-the page's own address}/join"
