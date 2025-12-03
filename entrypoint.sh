#!/usr/bin/env bash
set -euo pipefail

# defaults
DATA_DIR=${MONITOR_DATA_DIR:-/app/data}
URLS_FILE=${MONITOR_URLS_FILE:-/app/src/urls.txt}

# ensure data dir exists and has proper perms
mkdir -p "${DATA_DIR}"
chown -R root:root "${DATA_DIR}" || true

# if urls file doesn't exist in src, but exists in data, prefer data
if [ ! -f "${URLS_FILE}" ] && [ -f "${DATA_DIR}/urls.txt" ]; then
  URLS_FILE="${DATA_DIR}/urls.txt"
fi

export MONITOR_URLS_FILE="${URLS_FILE}"

# allow caller to run arbitrary commands: if first arg starts with '-', pass to python
if [[ "${1:0:1}" = "-" ]]; then
  exec python monitor_multi.py "$@"
else
  exec "$@"
fi
