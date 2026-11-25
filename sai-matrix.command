#!/bin/bash
# Starts Sai Matrix (if not already running) and opens it in the browser. macOS.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
PORT="${SAI_MATRIX_PORT:-8767}"
if ! lsof -i :"$PORT" >/dev/null 2>&1; then
  nohup python3 -I server.py >"${TMPDIR:-/tmp}/sai-matrix.log" 2>&1 &
fi
sleep 1.2
open "http://127.0.0.1:$PORT"
