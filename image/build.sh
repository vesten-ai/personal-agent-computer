#!/usr/bin/env bash
# Build the desktop image on an Incus host. Run as root on the host from a checkout of this repo,
# after host/setup.sh.
#
#   image/build.sh [alias]      default alias: pac-desktop/dev
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ALIAS=${1:-pac-desktop/dev}
VM=pac-build-$$
P=(--project pac)

cleanup() { incus delete -f "${P[@]}" "$VM" >/dev/null 2>&1 || true; }
trap cleanup EXIT

incus launch images:ubuntu/24.04/cloud "$VM" "${P[@]}" --vm --profile build \
  -c limits.cpu=4 -c limits.memory=8GiB -c security.secureboot=false -d root,size=20GiB
for _ in $(seq 1 150); do incus exec "${P[@]}" "$VM" -- test -e /run/systemd/system 2>/dev/null && break; sleep 2; done
incus exec "${P[@]}" "$VM" -- cloud-init status --wait >/dev/null 2>&1 || true

incus file push "${P[@]}" "$HERE/provision.sh" "$HERE/computerd.py" "$HERE/trust-gateway-ca.sh" "$VM/root/"
time incus exec "${P[@]}" "$VM" -- bash /root/provision.sh

# Seal: each VM made from the image gets its own machine-id and runs cloud-init afresh.
incus exec "${P[@]}" "$VM" -- sh -c 'rm -f /root/provision.sh /root/computerd.py /root/trust-gateway-ca.sh; cloud-init clean --logs; truncate -s0 /etc/machine-id; rm -f /var/lib/dbus/machine-id; fstrim -av >/dev/null 2>&1 || true'
incus stop "${P[@]}" "$VM"
incus image delete "$ALIAS" >/dev/null 2>&1 || true
time incus publish "${P[@]}" "$VM" --alias "$ALIAS" --compression zstd \
  description="Personal Agent Computer desktop ($(date -u +%Y-%m-%d))"
# incus publish gives the image an expiry date (a month out); agents are created from it, so it must stay.
incus image show "$ALIAS" | sed 's/^expires_at: .*/expires_at: 1970-01-01T00:00:00Z/' | incus image edit "$ALIAS"
incus image info "$ALIAS" | grep -E 'Fingerprint|Size'
