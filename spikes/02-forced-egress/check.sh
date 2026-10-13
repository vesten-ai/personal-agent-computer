#!/usr/bin/env bash
# Spike 2: can an agent VM get out without going through the gateway? Run as root inside an agent
# VM (root is the worst case: the agent has passwordless sudo). Every "bypass" line should say blocked.
#
# Usage: check.sh <gateway-ip> <host-bridge-ip> <host-public-ip> [other-agent-vm-ip]
set -uo pipefail
GW=${1:?gateway ip}; HOSTBR=${2:?host bridge ip}; HOSTPUB=${3:?host public ip}; PEER=${4:-}
IF=$(ip -4 route show default | awk '{print $5; exit}'); IF=${IF:-eth0}
MYIP=$(ip -4 -o addr show dev "$IF" | awk '{print $4; exit}')
fail=0

expect() {  # expect <want: open|blocked> <label> <command...>; curl -f so a proxy refusal (HTTP 403/502) counts as blocked
  local want=$1 label=$2; shift 2
  if timeout 8 "$@" >/dev/null 2>&1; then got=open; else got=blocked; fi
  if [ "$got" = "$want" ]; then echo "ok       $label ($got)"; else echo "FAIL     $label (want $want, got $got)"; fail=1; fi
}
tcp() { timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2"; }

echo "== baseline: the sanctioned path works"
expect open    "default route is the gateway"                   sh -c "ip -4 route show default | grep -q 'via $GW'"
expect open    "DNS through the gateway"                        getent hosts example.com
expect open    "HTTPS through the gateway (transparent proxy)"  curl -fsS -o /dev/null https://example.com

echo "== bypass attempts"
expect blocked "TCP to a public IP on a non-web port (1.1.1.1:22)"     tcp 1.1.1.1 22
expect blocked "TCP to DNS-over-TLS (1.1.1.1:853)"                     tcp 1.1.1.1 853
expect blocked "ICMP to a public IP"                                   ping -c1 -W3 1.1.1.1
expect blocked "IPv6 to the internet"                                  curl -6 -sS -o /dev/null https://example.com
expect blocked "host bridge address, SSH"                              tcp "$HOSTBR" 22
expect blocked "host bridge address, Incus API"                        tcp "$HOSTBR" 8443
expect blocked "host bridge address, DNS (host's dnsmasq)"             tcp "$HOSTBR" 53
expect blocked "host public address, SSH"                              tcp "$HOSTPUB" 22
expect blocked "host public address, HTTPS (via proxy)"                curl -fsS -o /dev/null -k "https://$HOSTPUB/"
expect blocked "cloud metadata address"                                curl -fsS -o /dev/null -m5 http://169.254.169.254/
expect blocked "gateway, a port that is not the proxy (22)"            tcp "$GW" 22
if [ -n "$PEER" ]; then
  expect blocked "another agent VM (ICMP)"                             ping -c1 -W3 "$PEER"
  expect blocked "another agent VM (TCP 5900)"                         tcp "$PEER" 5900
fi

echo "== root-level bypass attempts"
ip route replace default via "$HOSTBR" dev "$IF"
expect blocked "default route via the host bridge, then HTTPS by IP"   curl -sS -o /dev/null -k --resolve "example.com:443:93.184.215.14" https://example.com
expect blocked "default route via the host bridge, then TCP 1.1.1.1:443" tcp 1.1.1.1 443
ip route replace default via "$GW" dev "$IF"

ip addr add "$GW/24" dev "$IF" 2>/dev/null
ip addr del "$MYIP" dev "$IF"
ip route replace default via "$HOSTBR" dev "$IF" src "$GW"
expect blocked "spoof the gateway's IP and route via the host"         tcp 1.1.1.1 443
ip addr add "$MYIP" dev "$IF"; ip addr del "$GW/24" dev "$IF"
ip route replace default via "$GW" dev "$IF"

OLDMAC=$(cat /sys/class/net/$IF/address)
ip link set dev "$IF" address 02:00:00:00:00:99
expect blocked "spoofed MAC address, HTTPS"                            curl -sS -o /dev/null -m5 https://example.com
ip link set dev "$IF" address "$OLDMAC"
sleep 2
expect open    "back to normal after restoring address and route"     curl -sS -o /dev/null https://example.com

echo "== known gaps (reported, not failures)"
echo "note     the gateway resolves any name for the VM, so DNS queries can carry data out unless its resolver is restricted"
[ -S /dev/incus/sock ] && echo "note     /dev/incus/sock (Incus guest API) is present: never put secrets in instance config or user.* keys"
exit $fail
