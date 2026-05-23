#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-3001}"
HTTPS_PROXY_PORT="${HTTPS_PROXY_PORT:-3000}"
NGINX_CONF="$ROOT_DIR/nginx/local.conf"

port_is_available() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

echo "Stopping AI agent webapp stack..."

# Graceful nginx stop for this project config.
if command -v nginx >/dev/null 2>&1; then
  nginx -s quit -p "$ROOT_DIR/" -c "$NGINX_CONF" >/dev/null 2>&1 || true
fi
sleep 1

# If nginx still owns the proxy port, terminate project nginx master.
if ! port_is_available "$HTTPS_PROXY_PORT"; then
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "nginx: master process nginx -p $ROOT_DIR/" >/dev/null 2>&1 || true
  fi
fi
sleep 1

# Last resort: kill anything still listening on the proxy port.
if ! port_is_available "$HTTPS_PROXY_PORT" && command -v lsof >/dev/null 2>&1; then
  while IFS= read -r pid; do
    kill "$pid" >/dev/null 2>&1 || true
  done < <(lsof -t -nP -iTCP:"$HTTPS_PROXY_PORT" -sTCP:LISTEN 2>/dev/null || true)
fi

# Stop backend uvicorn started by this app.
if command -v pkill >/dev/null 2>&1; then
  pkill -f "uvicorn app.web_app_server:app --host 127.0.0.1 --port $BACKEND_PORT" >/dev/null 2>&1 || true
fi
sleep 1

# Last resort: clear any listener still on backend port.
if ! port_is_available "$BACKEND_PORT" && command -v lsof >/dev/null 2>&1; then
  while IFS= read -r pid; do
    kill "$pid" >/dev/null 2>&1 || true
  done < <(lsof -t -nP -iTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null || true)
fi

if port_is_available "$HTTPS_PROXY_PORT" && port_is_available "$BACKEND_PORT"; then
  echo "Done. Ports are free: HTTPS proxy $HTTPS_PROXY_PORT, backend $BACKEND_PORT."
else
  echo "Cleanup finished with listeners still present."
  echo "Check manually:"
  echo "  lsof -nP -iTCP:$HTTPS_PROXY_PORT -sTCP:LISTEN"
  echo "  lsof -nP -iTCP:$BACKEND_PORT -sTCP:LISTEN"
  exit 1
fi

