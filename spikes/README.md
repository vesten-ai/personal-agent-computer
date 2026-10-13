# Spikes

Four small experiments that decide whether the v1 architecture holds. Each one answers a single
question and records what it found next to its script.

| # | Question | Script | Status |
|---|----------|--------|--------|
| 1 | Which programs in the VM work through the gateway's TLS interception, and does the placeholder → credential swap reach the real API? (Chromium, Node, Python, Codex, Claude Code, gh, git, uv, pip, npm) | [`01-tls-swap/check.sh`](01-tls-swap/check.sh) | **done**: all 13 work, 5 need a CA env var ([findings](01-tls-swap/FINDINGS.md)) |
| 2 | Can an agent VM on Incus get out without going through the gateway, even as root? | [`02-forced-egress/check.sh`](02-forced-egress/check.sh) | **done**: 21/21, no bypass found ([findings](02-forced-egress/FINDINGS.md)) |
| 3 | Can one in-VM daemon serve both GPT's and Claude's computer-use formats on the same task? | [`03-two-computer-use/run.py`](03-two-computer-use/run.py) | **done for GPT**: task passed twice through one daemon; Claude toolset and OpenAI built-in tool tested offline only ([findings](03-two-computer-use/FINDINGS.md)) |
| 4 | How much memory does a desktop VM take, how long do stateful stop and wake take, and how many fit on one host? | [`04-density/measure.sh`](04-density/measure.sh) | **done**: ~1.45 GiB per idle desktop, 11 s cold boot, 16 s stateful wake ([findings](04-density/FINDINGS.md)) |

## Test topology

```
Incus host
├── pac-br0 (10.96.10.0/24)   agent bridge: VMs are port-isolated and IP/MAC-filtered;
│   │                          host nftables lets only the gateway leave the bridge
│   ├── gateway container 10.96.10.2   egress proxy (mitmproxy + gateway/egress/credential_swap.py),
│   │                                   DNS, default route for every VM (DHCP options)
│   └── agent VMs 10.96.10.100+        desktop image, CA of the gateway trusted, placeholders only
└── pac-build (10.96.11.0/24) image builds only, plain NAT
```

Set-up order: `host/setup-spike-host.sh` on the host, build the image (`image/provision.sh`),
start the gateway container (`gateway/deploy/egress-container.sh`), launch agent VMs and run
`image/trust-gateway-ca.sh` in each, then the spikes.

The egress addon also has a local check that needs only mitmproxy and curl:
`gateway/egress/test_local.sh`.
