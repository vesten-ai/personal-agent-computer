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
export PAC_SUBNET=${PAC_SUBNET:-10.96.10}
export PAC_BUILD_SUBNET=${PAC_BUILD_SUBNET:-10.96.11}
PAC_GW_IP="$PAC_SUBNET.2"
PAC_AGENT_NET="$PAC_SUBNET.0/24"
PAC_HOST_API="https://$PAC_SUBNET.1:8443"
GW_EXEC=(incus exec "${P[@]}" pac-gw --)
source "$HERE/host/lib/readiness.sh"
step() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
command -v incus >/dev/null || { echo "Incus is not installed: https://linuxcontainers.org/incus/docs/main/installing/" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null || { echo "qemu-system-x86_64 is required (Ubuntu: apt install qemu-system-x86 qemu-utils)" >&2; exit 1; }
[ -e /dev/kvm ] || { echo "no /dev/kvm: agent desktops are virtual machines and need KVM" >&2; exit 1; }

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 "$HERE/host/check-network.py" "$PAC_SUBNET" "$PAC_BUILD_SUBNET"

step "host: project, bridges, egress firewall"
bash "$HERE/host/setup.sh"

step "desktop image $IMAGE"
if incus image show "$IMAGE" >/dev/null 2>&1; then
  echo "already there (rebuild with image/build.sh $IMAGE)"
  # Images built before build.sh cleared it carry an expiry date; clear it here too.
  incus image show "$IMAGE" | sed 's/^expires_at: .*/expires_at: 1970-01-01T00:00:00Z/' | incus image edit "$IMAGE"
else
  bash "$HERE/image/build.sh" "$IMAGE"
fi

step "gateway container"
if ! incus info "${P[@]}" pac-gw >/dev/null 2>&1; then
  # The gateway profile carries cloud-init.network-config; the non-cloud image ignores it
  # and DHCP points this gateway back at itself.
  incus launch images:ubuntu/24.04/cloud pac-gw "${P[@]}" --profile gateway \
    -c limits.cpu=2 -c limits.memory=2GiB -c boot.autostart=true
fi
# start is idempotent for the stopped-container case; do not restart a running gateway.
if [ "$(incus list "${P[@]}" pac-gw --format csv -c s)" = STOPPED ]; then
  incus start "${P[@]}" pac-gw
fi
pac_wait_ready 60 2 "gateway DNS/network" "${GW_EXEC[@]}" getent hosts archive.ubuntu.com >/dev/null

step "egress proxy"
"${GW_EXEC[@]}" mkdir -p /opt/pac/egress /etc/pac
incus file push "${P[@]}" "$HERE/gateway/egress/credential_swap.py" pac-gw/opt/pac/egress/credential_swap.py >/dev/null
if ! "${GW_EXEC[@]}" test -f /etc/pac/egress-base.json; then
  # Agents may reach any public host, but never this host's own addresses.
  deny=$(ip -o addr show scope global | awk '{print $4}' | python3 -c '
import ipaddress, json, sys
print(json.dumps([str(ipaddress.ip_interface(l.strip()).ip) for l in sys.stdin if l.strip()]))')
  printf '{"default": "allow", "allow_hosts": [], "deny_hosts": [], "deny_networks": %s, "audit_log": "/var/log/pac/egress.jsonl", "dns": {"max_label": 0, "names_per_minute": 300}, "secrets": []}\n' "$deny" \
    > /tmp/pac-egress-base.json
  incus file push "${P[@]}" /tmp/pac-egress-base.json pac-gw/etc/pac/egress-base.json >/dev/null
  rm -f /tmp/pac-egress-base.json
fi
# Hosts installed before DNS limits existed get the default limits.
"${GW_EXEC[@]}" python3 -c '
import json; p = "/etc/pac/egress-base.json"; b = json.load(open(p))
if "dns" not in b:
    b["dns"] = {"max_label": 0, "names_per_minute": 300}; json.dump(b, open(p, "w"))'
"${GW_EXEC[@]}" sh -c 'test -f /etc/pac/egress-policy.json || cp /etc/pac/egress-base.json /etc/pac/egress-policy.json'
incus file push "${P[@]}" "$HERE/gateway/deploy/egress-container.sh" pac-gw/root/egress-container.sh >/dev/null
"${GW_EXEC[@]}" env PAC_GW_IP="$PAC_GW_IP" PAC_AGENT_NET="$PAC_AGENT_NET" bash /root/egress-container.sh

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
"${GW_EXEC[@]}" env PAC_JOIN_TOKEN="$token" PAC_HOST_API="$PAC_HOST_API" bash /opt/pac/src/gateway/deploy/gateway-container.sh
"${GW_EXEC[@]}" sh -c 'uv pip install -q --reinstall-package pac-gateway --python /opt/pac/gateway-venv/bin/python /opt/pac/src/gateway && systemctl restart pac-gateway'

step "pac command"
cat > /usr/local/bin/pac <<'SH'
#!/bin/sh
# Manage agents from this host: the pac CLI runs in the gateway container. Try `pac --help`.
exec incus exec --project pac pac-gw -- /opt/pac/gateway-venv/bin/pac "$@"
SH
chmod 0755 /usr/local/bin/pac
echo "installed /usr/local/bin/pac"

step "done"
cat <<MSG
The gateway listens on this host at 127.0.0.1:8090. From your computer:
    ssh -L 8090:127.0.0.1:8090 root@<this host>      then open http://127.0.0.1:8090
Sign in with the admin token:
    incus exec --project pac pac-gw -- grep PAC_ADMIN_TOKEN /etc/pac/gateway.env
Before the first agent, put a model key in /etc/pac/gateway.env and set the model in /etc/pac/gateway.toml:
    incus exec --project pac pac-gw -- sh -c 'echo PAC_MODEL_GPT_KEY=... >> /etc/pac/gateway.env'
    incus exec --project pac pac-gw -- systemctl restart pac-gateway
Or work from this host's terminal:
    pac create assistant && pac send assistant "Open a browser and tell me today's top headline"
MSG
