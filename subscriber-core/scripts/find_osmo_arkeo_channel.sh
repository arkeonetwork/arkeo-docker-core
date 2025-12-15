#!/usr/bin/env bash
# find_osmo_arkeo_channel.sh
# List Osmosis transfer channels and optionally filter to help locate the Arkeo channel.
# Usage:
#   scripts/find_osmo_arkeo_channel.sh [rpc_url] [optional_search_pattern]
#   or: OSMO_RPC=http://host:26657 scripts/find_osmo_arkeo_channel.sh [optional_search_pattern]
# Example:
#   scripts/find_osmo_arkeo_channel.sh http://127.0.0.1:26660 ARKEO

set -euo pipefail

RPC="${1:-${OSMO_RPC:-}}"
if [[ -z "${RPC}" ]]; then
  echo "Set OSMO_RPC or pass rpc_url as first arg (e.g., http://host:26657)." >&2
  exit 1
fi

SEARCH="${2:-${1:-}}"
if [[ "${SEARCH}" == "${RPC}" ]]; then
  SEARCH=""
fi
LIMIT=1000
PAGE=1
FOUND=0

echo "Querying channels from ${RPC} (transfer->transfer only)..."
while true; do
  echo "Scanning page ${PAGE}..." >&2
  OUT="$(osmosisd query ibc channel channels --node "${RPC}" --page "${PAGE}" --limit "${LIMIT}" -o json 2>/dev/null)" || break
  COUNT="$(printf '%s' "${OUT}" | jq '.channels | length')"
  if [[ "${COUNT}" -eq 0 ]]; then
    break
  fi
  MATCHES="$(printf '%s' "${OUT}" | jq -r --arg pat "${SEARCH,,}" '
    .channels[]
    | select(.port_id=="transfer" and .counterparty.port_id=="transfer")
    | select($pat=="" or (.channel_id|ascii_downcase|test($pat)) or (.counterparty.channel_id|ascii_downcase|test($pat)))
    | "OSMOSIS_CHANNEL_ID=\(.channel_id)  COUNTERPARTY=\(.counterparty.channel_id)  ORDERING=\(.ordering)  STATE=\(.state)"
  ' 2>/dev/null || true)"
  if [[ -n "${MATCHES}" ]]; then
    echo "${MATCHES}"
    PAGE_MATCHES=$(printf '%s\n' "${MATCHES}" | wc -l | tr -d ' ')
    FOUND=$((FOUND + PAGE_MATCHES))
    echo "Page ${PAGE}: matched ${PAGE_MATCHES}, total so far ${FOUND}" >&2
  fi
  ((PAGE++))
done
echo "Found ${FOUND} matching transfer channels."
