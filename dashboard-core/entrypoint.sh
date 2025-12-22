#!/usr/bin/env bash
set -e

echo "Dashboard-core (cache sync + web UI only)"

ARKEOD_HOME=${ARKEOD_HOME:-/root/.arkeo}
ARKEOD_HOME=${ARKEOD_HOME/#\~/$HOME}
ARKEOD_NODE=${ARKEOD_NODE:-tcp://127.0.0.1:26657}
ADMIN_PORT=${ADMIN_PORT:-${ENV_ADMIN_PORT:-8077}}
ADMIN_API_PORT=${ADMIN_API_PORT:-${ENV_ADMIN_API_PORT:-9996}}
HTTP_PORT=${HTTP_PORT:-80}
HTTPS_PORT=${HTTPS_PORT:-443}
ENABLE_TLS=${ENABLE_TLS:-1}
TLS_CERT_PATH=${TLS_CERT_PATH:-/app/config/tls.crt}
TLS_KEY_PATH=${TLS_KEY_PATH:-/app/config/tls.key}
TLS_CERT_CN=${TLS_CERT_CN:-localhost}
TLS_SELF_SIGNED=${TLS_SELF_SIGNED:-1}
CACHE_DIR=${CACHE_DIR:-/app/cache}
CACHE_INIT_ON_START=${CACHE_INIT_ON_START:-1}
CACHE_INIT_TIMEOUT=${CACHE_INIT_TIMEOUT:-120}
CACHE_FETCH_INTERVAL=${CACHE_FETCH_INTERVAL:-300}
BLOCK_HEIGHT_INTERVAL=${BLOCK_HEIGHT_INTERVAL:-60}
BLOCK_TIME_SECONDS=${BLOCK_TIME_SECONDS:-5.79954919}

echo "Using:"
echo "  ARKEOD_HOME          = $ARKEOD_HOME"
echo "  ARKEOD_NODE          = $ARKEOD_NODE"
echo "  ADMIN_PORT           = $ADMIN_PORT"
echo "  ADMIN_API_PORT       = $ADMIN_API_PORT"
echo "  HTTP_PORT            = $HTTP_PORT"
echo "  HTTPS_PORT           = $HTTPS_PORT"
echo "  ENABLE_TLS           = $ENABLE_TLS"
echo "  TLS_CERT_PATH        = $TLS_CERT_PATH"
echo "  TLS_KEY_PATH         = $TLS_KEY_PATH"
echo "  TLS_CERT_CN          = $TLS_CERT_CN"
echo "  TLS_SELF_SIGNED      = $TLS_SELF_SIGNED"
echo "  CACHE_DIR            = $CACHE_DIR"
echo "  CACHE_INIT_ON_START  = $CACHE_INIT_ON_START"
echo "  CACHE_INIT_TIMEOUT   = ${CACHE_INIT_TIMEOUT}s"
echo "  CACHE_FETCH_INTERVAL = ${CACHE_FETCH_INTERVAL}s"
echo "  BLOCK_HEIGHT_INTERVAL= ${BLOCK_HEIGHT_INTERVAL}s"
echo "  BLOCK_TIME_SECONDS   = ${BLOCK_TIME_SECONDS}"

# Ensure home directory exists and point ~/.arkeo at ARKEOD_HOME
mkdir -p "$ARKEOD_HOME"
if [ "$ARKEOD_HOME" != "$HOME/.arkeo" ]; then
  ln -sfn "$ARKEOD_HOME" "$HOME/.arkeo"
fi

mkdir -p /app/config
mkdir -p "$CACHE_DIR"
mkdir -p /var/run /var/log/supervisor /run/nginx
rm -f /etc/nginx/sites-enabled/default

TLS_ACTIVE=0
if [ "$ENABLE_TLS" = "1" ] || [ "$ENABLE_TLS" = "true" ]; then
  if [ -f "$TLS_CERT_PATH" ] && [ -f "$TLS_KEY_PATH" ]; then
    TLS_ACTIVE=1
  elif [ "$TLS_SELF_SIGNED" != "0" ]; then
    mkdir -p "$(dirname "$TLS_CERT_PATH")" "$(dirname "$TLS_KEY_PATH")"
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
      -keyout "$TLS_KEY_PATH" -out "$TLS_CERT_PATH" -subj "/CN=$TLS_CERT_CN" >/dev/null 2>&1 || true
    if [ -f "$TLS_CERT_PATH" ] && [ -f "$TLS_KEY_PATH" ]; then
      TLS_ACTIVE=1
    fi
  fi
fi

cat > /etc/nginx/conf.d/dashboard.conf <<EOF
server {
  listen ${HTTP_PORT} default_server;
  server_name _;

  location /api/ {
    proxy_pass http://127.0.0.1:${ADMIN_API_PORT};
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }

  location / {
    proxy_pass http://127.0.0.1:${ADMIN_PORT};
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF

if [ "$TLS_ACTIVE" = "1" ]; then
  cat >> /etc/nginx/conf.d/dashboard.conf <<EOF
server {
  listen ${HTTPS_PORT} ssl http2 default_server;
  server_name _;

  ssl_certificate ${TLS_CERT_PATH};
  ssl_certificate_key ${TLS_KEY_PATH};
  ssl_protocols TLSv1.2 TLSv1.3;
  ssl_prefer_server_ciphers off;

  location /api/ {
    proxy_pass http://127.0.0.1:${ADMIN_API_PORT};
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }

  location / {
    proxy_pass http://127.0.0.1:${ADMIN_PORT};
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF
fi

INIT_LOG=/var/log/dashboard-init.log
if [ "${CACHE_INIT_ON_START}" != "0" ]; then
  echo "[init] Triggering initial cache sync (timeout ${CACHE_INIT_TIMEOUT}s)..."
  {
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] cache init start (timeout=${CACHE_INIT_TIMEOUT}s)"
  } >> "$INIT_LOG" 2>&1
  (
    if timeout "${CACHE_INIT_TIMEOUT}" python3 /app/cache_fetcher.py --once >> /var/log/dashboard-cache.log 2>&1; then
      echo "[init] Initial cache sync complete." | tee -a "$INIT_LOG"
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] cache init success" >> "$INIT_LOG" 2>&1
    else
      echo "[init] Initial cache sync failed or timed out (continuing; supervisor will keep cache_fetcher running)." | tee -a "$INIT_LOG"
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] cache init failed" >> "$INIT_LOG" 2>&1
    fi
  ) &
else
  echo "[init] Skipping initial cache sync (CACHE_INIT_ON_START=0)."
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] cache init skipped" >> "$INIT_LOG" 2>&1
fi

export ENV_ADMIN_PORT="$ADMIN_PORT"
export ENV_ADMIN_API_PORT="$ADMIN_API_PORT"

echo "Starting supervisord..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
