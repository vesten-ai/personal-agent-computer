#!/usr/bin/env bash
# Install Personal Agent Computer on an Incus host. Run as root from a checkout of this repository:
#
#   sudo ./install.sh
#
# Needs: Ubuntu 24.04 (or similar) with Incus 6.x and KVM, about 30 GB free in the default storage pool.
# Safe to run again: every step checks what is already there. It creates, in Incus project "pac":
#   - the agent bridge, its egress firewall and profiles          (host/setup.sh)
#   - the desktop image pac-desktop/dev                          (image/build.sh, ~5 min the first time)
#   - the gateway container pac-gw: egress proxy + gateway service, published on 127.0.0.1:8090
# Then add a model key to /etc/pac/gateway.env in the gateway container (it tells you how).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
P=(--project pac)
IMAGE=${PAC_IMAGE:-pac-desktop/dev}
GW_EXEC=(incus exec "${P[@]}" pac-gw --)
step() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
command -v incus >/dev/null || { echo "Incus is not installed: https://linuxcontainers.org/incus/docs/main/installing/" >&2; exit 1; }
[ -e /dev/kvm ] || { echo "no /dev/kvm: agent desktops are virtual machines and need KVM" >&2; exit 1; }

step "host: project, bridges, egress firewall"
bash "$HERE/host/setup.sh"

step "desktop image $IMAGE"
if incus image show "$IMAGE" >/dev/null 2>&1; then
  echo "already there (rebuild with image/build.sh $IMAGE)"
else
  bash "$HERE/image/build.sh" "$IMAGE"
fi

step "gateway container"
if ! incus info "${P[@]}" pac-gw >/dev/null 2>&1; then
  incus launch images:ubuntu/24.04 pac-gw "${P[@]}" --profile gateway \
    -c limits.cpu=2 -c limits.memory=2GiB -c boot.autostart=true
fi
for _ in $(seq 1 60); do "${GW_EXEC[@]}" getent hosts archive.ubuntu.com >/dev/null 2>&1 && break; sleep 2; done

step "egress proxy"
"${GW_EXEC[@]}" mkdir -p /opt/pac/egress /etc/pac
incus file push "${P[@]}" "$HERE/gateway/egress/credential_swap.py" pac-gw/opt/pac/egress/credential_swap.py >/dev/null
if ! "${GW_EXEC[@]}" test -f /etc/pac/egress-base.json; then
  # Agents may reach any public host, but never this host's own addresses.
  deny=$(ip -o addr show scope global | awk '{print $4}' | python3 -c '
import ipaddress, json, sys
print(json.dumps([str(ipaddress.ip_interface(l.strip()).ip) for l in sys.stdin if l.strip()]))')
  printf '{"default": "allow", "allow_hosts": [], "deny_hosts": [], "deny_networks": %s, "audit_log": "/var/log/pac/egress.jsonl", "secrets": []}\n' "$deny" \
    > /tmp/pac-egress-base.json
  incus file push "${P[@]}" /tmp/pac-egress-base.json pac-gw/etc/pac/egress-base.json >/dev/null
  rm -f /tmp/pac-egress-base.json
fi
"${GW_EXEC[@]}" sh -c 'test -f /etc/pac/egress-policy.json || cp /etc/pac/egress-base.json /etc/pac/egress-policy.json'
incus file push "${P[@]}" "$HERE/gateway/deploy/egress-container.sh" pac-gw/root/egress-container.sh >/dev/null
"${GW_EXEC[@]}" bash /root/egress-container.sh

step "gateway service"
"${GW_EXEC[@]}" rm -rf /opt/pac/src/gateway
"${GW_EXEC[@]}" mkdir -p /opt/pac/src
incus file push -r "${P[@]}" "$HERE/gateway" pac-gw/opt/pac/src/ >/dev/null
"${GW_EXEC[@]}" rm -rf /opt/pac/src/gateway/.venv
if "${GW_EXEC[@]}" sh -c 'incus remote list --format csv 2>/dev/null | grep -q "^host,"'; then
  incus config device show "${P[@]}" pac-gw | grep -q '^web:' || bash "$HERE/host/enable-gateway-api.sh" >/dev/null
  token=""
else
  token=$(bash "$HERE/host/enable-gateway-api.sh" | tail -1)
fi
"${GW_EXEC[@]}" env PAC_JOIN_TOKEN="$token" bash /opt/pac/src/gateway/deploy/gateway-container.sh
"${GW_EXEC[@]}" sh -c 'uv pip install -q --reinstall-package pac-gateway --python /opt/pac/gateway-venv/bin/python /opt/pac/src/gateway && systemctl restart pac-gateway'

step "done"
cat <<MSG
The gateway listens on this host at 127.0.0.1:8090. From your computer:
    ssh -L 8090:127.0.0.1:8090 root@<this host>      then open http://127.0.0.1:8090
Sign in with the admin token:
    incus exec --project pac pac-gw -- grep PAC_ADMIN_TOKEN /etc/pac/gateway.env
Before the first agent, put a model key in /etc/pac/gateway.env and set the model in /etc/pac/gateway.toml:
    incus exec --project pac pac-gw -- sh -c 'echo PAC_MODEL_GPT_KEY=... >> /etc/pac/gateway.env'
    incus exec --project pac pac-gw -- systemctl restart pac-gateway
MSG
