#!/usr/bin/env bash
# Destructive test fixture: use only on a disposable host without an existing pac project.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export PAC_SUBNET=${PAC_SUBNET:-10.88.10}
export PAC_BUILD_SUBNET=${PAC_BUILD_SUBNET:-10.88.11}
if incus project show pac >/dev/null 2>&1; then
  echo 'Acceptance requires a disposable host without an existing pac project' >&2
  exit 1
fi
bash "$HERE/install.sh"
python3 "$HERE/tests/acceptance/check-install.py"
G=(incus exec --project pac pac-gw --)
before=$("${G[@]}" sha256sum /etc/pac/gateway.env /etc/pac/gateway.toml)
incus stop --project pac pac-gw
bash "$HERE/install.sh"
after=$("${G[@]}" sha256sum /etc/pac/gateway.env /etc/pac/gateway.toml)
[ "$before" = "$after" ] || { echo 'Rerun changed credentials/configuration' >&2; exit 1; }
"${G[@]}" systemctl is-active --quiet pac-gateway
# Only invoke the non-mutating preflight with a conflicting prefix.
if python3 "$HERE/host/check-network.py" 10.89.10 10.89.11; then
  echo 'Conflicting subnet was accepted' >&2
  exit 1
fi
echo 'PASS: first install, stopped gateway rerun, config preservation, subnet conflict refusal'
