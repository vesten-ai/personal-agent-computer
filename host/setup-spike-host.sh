#!/usr/bin/env bash
# Prepare an Incus host for Personal Agent Computer spikes. Run as root on the host.
#
# Creates, all idempotently:
#   - Incus project "pac" (own profiles and volumes, shares the host's images and networks)
#   - bridge pac-br0  (agent VMs; only the gateway at .2 may leave the bridge)
#   - bridge pac-build (image builds; plain NAT, no agent ever runs here)
#   - nftables table inet pac_egress, which enforces the pac-br0 rule on the host
#
# Agent VMs get the gateway as their default route and DNS server via DHCP, and their
# NICs have port isolation plus IP/MAC filtering, so they can't talk to each other or
# pose as the gateway.
set -euo pipefail

SUBNET=${PAC_SUBNET:-10.96.10}          # agent bridge, /24
BUILD_SUBNET=${PAC_BUILD_SUBNET:-10.96.11}
GW_IP="$SUBNET.2"
POOL=${PAC_POOL:-default}

incus project show pac >/dev/null 2>&1 || incus project create pac \
  -c features.images=false -c features.profiles=true \
  -c features.storage.volumes=true -c features.networks=false

if ! incus network show pac-br0 >/dev/null 2>&1; then
  incus network create pac-br0 --project default \
    ipv4.address="$SUBNET.1/24" ipv4.nat=true ipv6.address=none \
    ipv4.dhcp.ranges="$SUBNET.100-$SUBNET.199" \
    ipv4.dhcp.gateway="$GW_IP" \
    raw.dnsmasq="dhcp-option=option:dns-server,$GW_IP"
fi
if ! incus network show pac-build >/dev/null 2>&1; then
  incus network create pac-build --project default \
    ipv4.address="$BUILD_SUBNET.1/24" ipv4.nat=true ipv6.address=none
fi

# Profiles inside the project.
#   default: agent VMs on pac-br0, isolated from each other, can't spoof addresses.
#   gateway: the gateway's NIC is not port-isolated, so every agent VM can reach it.
#   build:   image builds on pac-build.
incus profile show default --project pac | grep -q 'network: pac-br0' || incus profile edit default --project pac <<YAML
description: Personal Agent Computer agent VM
config: {}
devices:
  root: {type: disk, path: /, pool: $POOL}
  eth0:
    type: nic
    network: pac-br0
    name: eth0
    security.port_isolation: "true"
    security.mac_filtering: "true"
    security.ipv4_filtering: "true"
YAML
incus profile show gateway --project pac >/dev/null 2>&1 || incus profile create gateway --project pac
# The DHCP options above point every client at the gateway, so the gateway itself gets a
# static configuration that routes through the host bridge instead.
incus profile edit gateway --project pac <<YAML
description: Personal Agent Computer gateway
config:
  cloud-init.network-config: |
    network:
      version: 2
      ethernets:
        eth0:
          dhcp4: false
          addresses: [$GW_IP/24]
          routes: [{to: default, via: $SUBNET.1}]
          nameservers: {addresses: [$SUBNET.1]}
devices:
  root: {type: disk, path: /, pool: $POOL}
  eth0:
    type: nic
    network: pac-br0
    name: eth0
    ipv4.address: $GW_IP
    # No ipv4_filtering here: the gateway answers transparently-proxied connections with the
    # original destination as source address, which IP filtering would drop as spoofed.
    security.mac_filtering: "true"
YAML
incus profile show build --project pac >/dev/null 2>&1 || incus profile create build --project pac
incus profile edit build --project pac <<YAML
description: Personal Agent Computer image build
config: {}
devices:
  root: {type: disk, path: /, pool: $POOL}
  eth0: {type: nic, network: pac-build, name: eth0}
YAML

# Host-side egress rule for pac-br0. Base chains at the same hook all run, and a drop in
# any of them is final, so this holds no matter what Incus's own table accepts.
install -d /etc/pac
cat > /etc/pac/egress.nft <<NFT
table inet pac_egress {}
delete table inet pac_egress
table inet pac_egress {
  chain forward {
    type filter hook forward priority -5; policy accept;
    iifname "pac-br0" ip saddr $GW_IP accept
    iifname "pac-br0" drop
    oifname "pac-br0" ip daddr $GW_IP accept
    oifname "pac-br0" drop
  }
  chain input {
    type filter hook input priority -5; policy accept;
    iifname "pac-br0" ip saddr $GW_IP accept
    iifname "pac-br0" udp dport 67 accept
    iifname "pac-br0" drop
  }
}
NFT
nft -f /etc/pac/egress.nft
cat > /etc/systemd/system/pac-egress.service <<UNIT
[Unit]
Description=Personal Agent Computer: only the gateway may leave pac-br0
After=network-pre.target
Before=incus.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/pac/egress.nft
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable pac-egress.service >/dev/null 2>&1

echo "pac host ready: agent bridge $SUBNET.0/24, gateway $GW_IP, build bridge $BUILD_SUBNET.0/24"
