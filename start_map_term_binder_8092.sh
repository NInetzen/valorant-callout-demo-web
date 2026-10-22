#!/bin/bash
set -e
APP_DIR=/data/workspace/map-term-binder
PORT=8092
mkdir -p "$APP_DIR"
if command -v fuser >/dev/null 2>&1; then
  fuser -k ${PORT}/tcp 2>/dev/null || true
else
  pkill -f "http.server ${PORT}" 2>/dev/null || true
fi
sleep 1
cd "$APP_DIR"
cp -f map_term_binder.html index.html
nohup /usr/bin/python3 -m http.server ${PORT} --bind 0.0.0.0 > "$APP_DIR/server.log" 2>&1 &
echo "started pid=$! port=${PORT} dir=${APP_DIR}"
