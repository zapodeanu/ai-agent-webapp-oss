#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_LOGGER_NAME="${APP_LOGGER_NAME:-ai_agent_webapp_launcher}"
_BANNER_LINE="================================================================================"

timestamp_now() {
  python3 - <<'PY'
from datetime import datetime
print(datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3])
PY
}

log_info() {
  local message="$1"
  printf "%s - PID:%s - %s - INFO - %s\n" "$(timestamp_now)" "$$" "$APP_LOGGER_NAME" "$message"
}

if [[ -d ".venv" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed."
  echo "Install with: brew install nginx"
  exit 1
fi

BACKEND_PORT="${BACKEND_PORT:-3001}"
HTTPS_PROXY_PORT="${HTTPS_PROXY_PORT:-3000}"
BACKEND_RELOAD="${BACKEND_RELOAD:-false}"
CERT_DIR="${CERT_DIR:-certs}"
CERT_KEY="${CERT_KEY:-$CERT_DIR/local-dev.key}"
CERT_CRT="${CERT_CRT:-$CERT_DIR/local-dev.crt}"
NGINX_CONF="$ROOT_DIR/nginx/local.conf"
NGINX_TEMPLATE="$ROOT_DIR/nginx/local.conf.template"
NGINX_MIME_TYPES="${NGINX_MIME_TYPES:-}"
WEBAPP_LOG_FILE="${WEBAPP_LOG_FILE:-$ROOT_DIR/logs/ai_agent_webapp.log}"
APP_LOG_FORMAT="${APP_LOG_FORMAT:-${MCP_LOG_FORMAT:-%(asctime)s - PID:%(process)d - %(name)s - %(levelname)s - %(message)s}}"
UVICORN_LOG_CONFIG="$ROOT_DIR/nginx/uvicorn.log.config.json"

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

stop_existing_stack() {
  # Stop prior webapp nginx instance (if any) using this config.
  nginx -s quit -p "$ROOT_DIR/" -c "$NGINX_CONF" >/dev/null 2>&1 || true
  sleep 1

  # Fallback: if nginx is still listening, terminate the project-specific master
  # process started with this root prefix. This handles cases where nginx cannot
  # find a pid file for `nginx -s quit`.
  if ! port_is_available "$HTTPS_PROXY_PORT"; then
    if command -v pkill >/dev/null 2>&1; then
      pkill -f "nginx: master process nginx -p $ROOT_DIR/" >/dev/null 2>&1 || true
    fi
  fi

  # Last-resort cleanup: terminate any listener still bound to the HTTPS proxy port.
  if ! port_is_available "$HTTPS_PROXY_PORT" && command -v lsof >/dev/null 2>&1; then
    while IFS= read -r pid; do
      kill "$pid" >/dev/null 2>&1 || true
    done < <(lsof -t -nP -iTCP:"$HTTPS_PROXY_PORT" -sTCP:LISTEN 2>/dev/null || true)
    sleep 1
  fi

  # Stop prior backend instance on the same configured backend port.
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "uvicorn app.web_app_server:app --host 127.0.0.1 --port $BACKEND_PORT" >/dev/null 2>&1 || true
  fi
}

mkdir -p "$CERT_DIR" "$ROOT_DIR/logs" "$ROOT_DIR/nginx"

if [[ "${WEBAPP_LOG_FILE#/}" == "$WEBAPP_LOG_FILE" ]]; then
  WEBAPP_LOG_FILE="$ROOT_DIR/$WEBAPP_LOG_FILE"
fi
mkdir -p "$(dirname "$WEBAPP_LOG_FILE")"

if [[ "${CERT_KEY#/}" == "$CERT_KEY" ]]; then
  CERT_KEY="$ROOT_DIR/$CERT_KEY"
fi
if [[ "${CERT_CRT#/}" == "$CERT_CRT" ]]; then
  CERT_CRT="$ROOT_DIR/$CERT_CRT"
fi
mkdir -p "$(dirname "$CERT_KEY")" "$(dirname "$CERT_CRT")"

if [[ ! -f "$CERT_KEY" || ! -f "$CERT_CRT" ]]; then
  echo "TLS cert/key not found. Generating local dev certificate..."
  openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
    -keyout "$CERT_KEY" \
    -out "$CERT_CRT" \
    -subj "/CN=localhost"
fi

esc() {
  printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'
}

if [[ -z "$NGINX_MIME_TYPES" ]]; then
  for candidate in \
    "/opt/homebrew/etc/nginx/mime.types" \
    "/usr/local/etc/nginx/mime.types" \
    "/etc/nginx/mime.types"; do
    if [[ -f "$candidate" ]]; then
      NGINX_MIME_TYPES="$candidate"
      break
    fi
  done
fi

if [[ -z "$NGINX_MIME_TYPES" ]]; then
  echo "Could not locate nginx mime.types."
  echo "Set it explicitly, e.g.:"
  echo "  NGINX_MIME_TYPES=/opt/homebrew/etc/nginx/mime.types bash scripts/start_with_nginx.sh"
  exit 1
fi

ROOT_ESCAPED="$(esc "$ROOT_DIR")"
KEY_ESCAPED="$(esc "$CERT_KEY")"
CRT_ESCAPED="$(esc "$CERT_CRT")"
MIME_ESCAPED="$(esc "$NGINX_MIME_TYPES")"
LOG_ESCAPED="$(esc "$WEBAPP_LOG_FILE")"

sed \
  -e "s/__ROOT__/$ROOT_ESCAPED/g" \
  -e "s/__BACKEND_PORT__/$BACKEND_PORT/g" \
  -e "s/__HTTPS_PROXY_PORT__/$HTTPS_PROXY_PORT/g" \
  -e "s/__CERT_KEY__/$KEY_ESCAPED/g" \
  -e "s/__CERT_CRT__/$CRT_ESCAPED/g" \
  -e "s/__NGINX_MIME_TYPES__/$MIME_ESCAPED/g" \
  -e "s/__APP_LOG_FILE__/$LOG_ESCAPED/g" \
  "$NGINX_TEMPLATE" > "$NGINX_CONF"

APP_LOG_FORMAT_JSON="${APP_LOG_FORMAT//\"/\\\"}"
cat > "$UVICORN_LOG_CONFIG" <<EOF
{
  "version": 1,
  "disable_existing_loggers": false,
  "formatters": {
    "default": {
      "()": "uvicorn.logging.DefaultFormatter",
      "fmt": "$APP_LOG_FORMAT_JSON",
      "use_colors": false
    },
    "access": {
      "()": "uvicorn.logging.AccessFormatter",
      "fmt": "$APP_LOG_FORMAT_JSON",
      "use_colors": false
    }
  },
  "handlers": {
    "default": {
      "class": "logging.StreamHandler",
      "formatter": "default",
      "stream": "ext://sys.stderr"
    },
    "access": {
      "class": "logging.StreamHandler",
      "formatter": "access",
      "stream": "ext://sys.stdout"
    }
  },
  "loggers": {
    "uvicorn": {
      "handlers": ["default"],
      "level": "INFO",
      "propagate": false
    },
    "uvicorn.error": {
      "level": "INFO"
    },
    "uvicorn.access": {
      "handlers": ["access"],
      "level": "WARNING",
      "propagate": false
    }
  }
}
EOF

# Auto-restart behavior: if target ports are already in use, attempt to stop the
# previous stack for this project and start cleanly.
if ! port_is_available "$BACKEND_PORT" || ! port_is_available "$HTTPS_PROXY_PORT"; then
  echo "Existing listener detected. Attempting automatic restart cleanup..."
  stop_existing_stack
  sleep 1
fi

cleanup() {
  if [[ "${BACKEND_STARTED:-false}" == "true" || "${NGINX_STARTED:-false}" == "true" ]]; then
    echo "Stopping nginx/backend services..."
  fi
  if [[ "${BACKEND_STARTED:-false}" == "true" && -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ "${NGINX_STARTED:-false}" == "true" ]]; then
    nginx -s quit -p "$ROOT_DIR/" -c "$NGINX_CONF" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

if ! port_is_available "$BACKEND_PORT"; then
  echo "ERROR: backend port $BACKEND_PORT is already in use."
  echo "Try: BACKEND_PORT=3101 bash scripts/start_with_nginx.sh"
  exit 1
fi
if ! port_is_available "$HTTPS_PROXY_PORT"; then
  log_info "ERROR: nginx HTTPS proxy port $HTTPS_PROXY_PORT is already in use."
  log_info "Try: HTTPS_PROXY_PORT=3443 bash scripts/start_with_nginx.sh"
  exit 1
fi
log_info "$_BANNER_LINE"
log_info "AI Agent Webapp Started (nginx + uvicorn)"
log_info "Process ID: $$"
log_info "Starting backend on 127.0.0.1:$BACKEND_PORT"
UVICORN_CMD=(
  uvicorn app.web_app_server:app
  --host 127.0.0.1
  --port "$BACKEND_PORT"
  --log-config "$UVICORN_LOG_CONFIG"
  --proxy-headers
  --forwarded-allow-ips="*"
)
if [[ "$BACKEND_RELOAD" == "true" ]]; then
  UVICORN_CMD+=(--reload)
fi
ENABLE_HSTS=false APP_LOG_FORMAT="$APP_LOG_FORMAT" "${UVICORN_CMD[@]}" >>"$WEBAPP_LOG_FILE" 2>&1 &
BACKEND_PID=$!
BACKEND_STARTED=true

log_info "Starting nginx TLS proxy..."
log_info "HTTPS proxy: https://localhost:$HTTPS_PROXY_PORT -> backend:$BACKEND_PORT"
log_info "$_BANNER_LINE"
nginx -p "$ROOT_DIR/" -c "$NGINX_CONF"
NGINX_STARTED=true

while true; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" || true
    break
  fi
  sleep 1
done
