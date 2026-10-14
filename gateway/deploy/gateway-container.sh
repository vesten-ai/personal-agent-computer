#!/usr/bin/env bash
# Install the gateway service in the gateway container (Ubuntu 24.04), next to the egress proxy.
# Run as root in the container with the repository's gateway/ directory at /opt/pac/src/gateway:
#
#   PAC_JOIN_TOKEN=<token from host/enable-gateway-api.sh> bash gateway-container.sh
#
# Secrets go in /etc/pac/gateway.env (PAC_ADMIN_TOKEN, one key per model), mode 0600.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
HOST_API=${PAC_HOST_API:-https://10.96.10.1:8443}

apt-get update -qq
apt-get install -y -qq --no-install-recommends incus-client python3 python3-venv ca-certificates >/dev/null

# Incus remote "host", restricted to project pac by the host.
if ! incus remote list --format csv | grep -q '^host,'; then
  : "${PAC_JOIN_TOKEN:?set PAC_JOIN_TOKEN (from host/enable-gateway-api.sh)}"
  incus remote add host "$HOST_API" --token "$PAC_JOIN_TOKEN" --accept-certificate --quiet
fi
incus list host: --project pac --format csv >/dev/null

uv venv -q --allow-existing /opt/pac/gateway-venv
uv pip install -q --python /opt/pac/gateway-venv/bin/python /opt/pac/src/gateway

install -d -m 0755 /etc/pac /var/lib/pac
[ -f /etc/pac/gateway.toml ] || install -m 0644 /opt/pac/src/gateway/gateway.example.toml /etc/pac/gateway.toml
if [ ! -f /etc/pac/gateway.env ]; then
  umask 077
  printf 'PAC_ADMIN_TOKEN=%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /etc/pac/gateway.env
  echo "wrote /etc/pac/gateway.env with a new admin token; add the model keys there"
fi

cat > /etc/systemd/system/pac-gateway.service <<'UNIT'
[Unit]
Description=Personal Agent Computer gateway (agent loop, web chat)
After=network-online.target pac-egress.service
Wants=network-online.target
[Service]
EnvironmentFile=/etc/pac/gateway.env
Environment=HOME=/root
ExecStart=/opt/pac/gateway-venv/bin/pac-gateway /etc/pac/gateway.toml
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now pac-gateway.service >/dev/null 2>&1
systemctl restart pac-gateway.service
# is-active alone can briefly succeed before startup fails. Check the HTTP listener too.
ready=false
for _ in $(seq 1 30); do
  if systemctl is-active --quiet pac-gateway.service && /opt/pac/gateway-venv/bin/python - <<'PYTHON'
import sys
import tomllib
import urllib.error
import urllib.request
with open('/etc/pac/gateway.toml', 'rb') as f:
    config = tomllib.load(f)
port = config.get('listen_port', 8000)
try:
    urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2).close()
except urllib.error.HTTPError as e:
    if e.code != 401:
        sys.exit(1)
except urllib.error.URLError:
    sys.exit(1)
PYTHON
  then ready=true; break; fi
  sleep 1
done
if [ "$ready" != true ]; then
  journalctl -u pac-gateway.service --no-pager -n 20 >&2
  echo "Gateway did not become ready" >&2
  exit 1
fi
echo active
