#!/usr/bin/env bash
# Set up the egress proxy inside the gateway container (Ubuntu 24.04). Run as root in the container.
# Expects the addon at /opt/pac/egress/credential_swap.py and the policy at /etc/pac/egress-policy.json.
#
# Agent VMs use this container as default gateway and DNS server (DHCP options on pac-br0), so:
#   tcp 80/443 from the agent subnet  -> mitmproxy transparent mode (:8081)
#   dns to any address                -> mitmproxy dns mode (:53)
#   explicit proxy                    -> mitmproxy regular mode (:8080), for clients that want HTTPS_PROXY
#   everything else from a VM         -> dropped (no IP forwarding, input policy drop)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
AGENT_NET=${PAC_AGENT_NET:-10.96.10.0/24}
GW_IP=${PAC_GW_IP:-10.96.10.2}   # DNS binds here: systemd-resolved already holds 127.0.0.53:53
MITM_VERSION=12.2.3

apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl nftables python3 >/dev/null
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null
UV_TOOL_DIR=/opt/pac/tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install --quiet "mitmproxy==$MITM_VERSION"

id pac-egress >/dev/null 2>&1 || useradd --system --home /var/lib/pac-egress --create-home --shell /usr/sbin/nologin pac-egress
install -d -o pac-egress -g pac-egress -m 0750 /var/lib/pac-egress/mitm /var/log/pac
install -d -m 0755 /etc/pac
[ -f /etc/pac/secrets.env ] || install -m 0600 /dev/null /etc/pac/secrets.env

sysctl -qw net.ipv4.ip_forward=0
echo 'net.ipv4.ip_forward=0' > /etc/sysctl.d/90-pac-egress.conf

cat > /etc/pac/egress-container.nft <<NFT
table inet pac_gw {}
delete table inet pac_gw
table inet pac_gw {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    ip saddr $AGENT_NET tcp dport { 80, 443 } redirect to :8081
    ip saddr $AGENT_NET udp dport 53 redirect to :53
    ip saddr $AGENT_NET tcp dport 53 redirect to :53
  }
  chain input {
    type filter hook input priority filter; policy accept;
    ip saddr $AGENT_NET tcp dport { 53, 8080, 8081 } accept
    ip saddr $AGENT_NET udp dport 53 accept
    ip saddr $AGENT_NET icmp type echo-request accept
    ip saddr $AGENT_NET ct state established,related accept
    ip saddr $AGENT_NET drop
  }
  chain forward {
    type filter hook forward priority filter; policy drop;
  }
}
NFT
nft -f /etc/pac/egress-container.nft

cat > /etc/systemd/system/pac-egress-fw.service <<'UNIT'
[Unit]
Description=PAC gateway: egress firewall
Before=pac-egress.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/pac/egress-container.nft
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/pac-egress.service <<UNIT
[Unit]
Description=PAC gateway: egress proxy (placeholder -> credential swap)
After=network-online.target pac-egress-fw.service
Wants=network-online.target
[Service]
User=pac-egress
EnvironmentFile=/etc/pac/secrets.env
AmbientCapabilities=CAP_NET_BIND_SERVICE
ExecStart=/usr/local/bin/mitmdump --set confdir=/var/lib/pac-egress/mitm \
  --mode transparent@8081 --mode regular@8080 --mode dns@$GW_IP:53 \
  --set block_global=false --set connection_strategy=lazy \
  -s /opt/pac/egress/credential_swap.py --set pac_policy=/etc/pac/egress-policy.json
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now pac-egress-fw.service pac-egress.service >/dev/null 2>&1
systemctl restart pac-egress.service
for i in $(seq 1 30); do [ -s /var/lib/pac-egress/mitm/mitmproxy-ca-cert.pem ] && break; sleep 1; done
echo "egress proxy up; CA at /var/lib/pac-egress/mitm/mitmproxy-ca-cert.pem"
