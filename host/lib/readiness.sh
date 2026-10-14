#!/usr/bin/env bash
# Wait for a bounded number of attempts; failure must stop installation.
pac_wait_ready() {
  local attempts=$1 delay=$2 label=$3
  shift 3
  local attempt
  for ((attempt=1; attempt<=attempts; attempt++)); do
    if "$@"; then
      return 0
    fi
    if ((attempt < attempts)); then sleep "$delay"; fi
  done
  echo "Timed out waiting for $label after $attempts attempts" >&2
  return 1
}
