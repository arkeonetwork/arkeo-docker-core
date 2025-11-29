#!/usr/bin/env bash
set -e

SENTINEL_CONFIG_PATH=${SENTINEL_CONFIG_PATH:-/app/config/sentinel.yaml}
SENTINEL_ENV_PATH=${SENTINEL_ENV_PATH:-/app/config/sentinel.env}

if [ -f "$SENTINEL_ENV_PATH" ]; then
  # Export all vars defined in sentinel.env so the sentinel binary sees them
  set -a
  # shellcheck disable=SC1090
  source "$SENTINEL_ENV_PATH"
  set +a
fi

exec /usr/local/bin/sentinel --config "$SENTINEL_CONFIG_PATH"
