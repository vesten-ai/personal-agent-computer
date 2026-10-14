#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/lib/readiness.sh"
calls=0
becomes_ready() { calls=$((calls+1)); [ "$calls" -ge 3 ]; }
pac_wait_ready 3 0 test becomes_ready
[ "$calls" = 3 ]
calls=0
never_ready() { calls=$((calls+1)); return 1; }
if pac_wait_ready 3 0 test never_ready 2>/dev/null; then
  echo 'failure was incorrectly accepted' >&2
  exit 1
fi
[ "$calls" = 3 ]
calls=0
if pac_wait_ready 0 0 test never_ready 2>/dev/null; then exit 1; fi
[ "$calls" = 0 ]
echo 'Readiness retry and failure checks passed'
