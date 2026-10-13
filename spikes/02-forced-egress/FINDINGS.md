# Spike 2 findings: forced egress on Incus

**Answer: yes, an agent VM on Incus can be forced through the gateway, even when the agent is root.**
All 21 checks pass (run 2026-10-13 on an Incus 6.0 host, Ubuntu 24.04, desktop image `pac-desktop/dev`).

## What enforces it

Three layers, each closing what the others can't:

| Layer | Rule | Stops |
|-------|------|-------|
| Incus NIC on each agent VM | `security.port_isolation`, `security.mac_filtering`, `security.ipv4_filtering` | VM-to-VM traffic; posing as the gateway or another VM |
| Host nftables (`inet pac_egress`) | only the gateway's address may be forwarded off the agent bridge; VMs may reach the host only for DHCP | a root agent that points its default route at the host bridge; reaching host services (SSH, Incus API, dnsmasq) |
| Gateway container | no IP forwarding; tcp 80/443 redirected to the transparent proxy, DNS to its own resolver; the proxy refuses internal, link-local and the host's own addresses | every non-HTTP protocol (raw TCP, ICMP, DNS-over-TLS, QUIC); SSRF to the host and metadata addresses |

DHCP on the agent bridge hands out the gateway as router and DNS server, so a VM that does nothing
special goes through the proxy without any proxy settings.

## Results

```
== baseline: the sanctioned path works
ok       default route is the gateway (open)
ok       DNS through the gateway (open)
ok       HTTPS through the gateway (transparent proxy) (open)
== bypass attempts
ok       TCP to a public IP on a non-web port (1.1.1.1:22) (blocked)
ok       TCP to DNS-over-TLS (1.1.1.1:853) (blocked)
ok       ICMP to a public IP (blocked)
ok       IPv6 to the internet (blocked)
ok       host bridge address, SSH (blocked)
ok       host bridge address, Incus API (blocked)
ok       host bridge address, DNS (host's dnsmasq) (blocked)
ok       host public address, SSH (blocked)
ok       host public address, HTTPS (via proxy) (blocked)
ok       cloud metadata address (blocked)
ok       gateway, a port that is not the proxy (22) (blocked)
ok       another agent VM (ICMP) (blocked)
ok       another agent VM (TCP 5900) (blocked)
== root-level bypass attempts
ok       default route via the host bridge, then HTTPS by IP (blocked)
ok       default route via the host bridge, then TCP 1.1.1.1:443 (blocked)
ok       spoof the gateway's IP and route via the host (blocked)
ok       spoofed MAC address, HTTPS (blocked)
ok       back to normal after restoring address and route (open)
```

## Things we learned the hard way

- **The gateway's own NIC must not have `security.ipv4_filtering`.** A transparent proxy answers
  with the original destination as source address, and Incus drops that as spoofing. Symptom: every
  HTTPS connection from a VM times out while a direct connection to the proxy port works.
- **The gateway needs a static network config.** The DHCP options that point VMs at the gateway also
  apply to the gateway, which would then route through itself.
- **systemd-resolved holds 127.0.0.53:53 in the gateway**, so the proxy's DNS listener binds the
  gateway's bridge address instead of `*:53`.

## Open gaps

- **DNS can carry data out.** The gateway resolves any name, so a determined agent can encode data in
  queries to a domain it controls. Closing it means an allow-list resolver or rate and entropy limits;
  v1 should at least log every query (the proxy's DNS mode can).
- **HTTPS to any public host is allowed by default** (`"default": "allow"` in the policy). That is the
  right default for a general-purpose agent computer, but a deployment can switch to deny-by-default.
- **`/dev/incus/sock` is visible in the VM.** It is the Incus guest API (instance metadata, not the host
  API). Never put secrets in instance config or `user.*` keys.
