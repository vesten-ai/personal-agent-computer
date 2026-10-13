#!/usr/bin/env bash
# Let the gateway container drive agent VMs through the Incus API, and nothing else on the host.
# Run as root on the Incus host after host/setup.sh. Prints a one-time join token for
# gateway/deploy/gateway-container.sh.
#
# - The API listens on the agent bridge address only. Agent VMs can't reach it: the host's
#   pac_egress table drops everything from the bridge except the gateway.
# - The gateway's client certificate is restricted to project "pac".
# - The gateway's web page is published on the host's loopback (127.0.0.1:8090) through an Incus
#   proxy device; put your own TLS reverse proxy or an SSH tunnel in front of it.
set -euo pipefail
SUBNET=${PAC_SUBNET:-10.96.10}
API="$SUBNET.1:8443"

current=$(incus config get core.https_address)
if [ -z "$current" ]; then
  incus config set core.https_address "$API"
elif [ "$current" != "$API" ]; then
  echo "core.https_address is already $current; leaving it. The gateway needs to reach it at a bridge address." >&2
fi

incus config device show --project pac pac-gw | grep -q '^web:' || \
  incus config device add --project pac pac-gw web proxy listen=tcp:127.0.0.1:8090 connect=tcp:127.0.0.1:8000

# One-time token; the gateway turns it into a client certificate restricted to project pac.
incus config trust add pac-gateway --projects pac --restricted --quiet
