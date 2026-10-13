# Installing

Personal Agent Computer runs on one Linux host with [Incus](https://linuxcontainers.org/incus/) and KVM.
Every agent gets its own desktop virtual machine on that host; a gateway container next to them runs
the agent loop, the web page and the egress proxy.

## What you need

- Ubuntu 24.04 (or another distribution with Incus 6.x), with KVM (`/dev/kvm`).
- Memory: about 1.5 GiB per idle agent with a browser open, plus 2 GiB for the gateway. Agents that
  sleep (see below) use none.
- Disk: about 2 GB for the desktop image and 20 GB per agent (thin-provisioned on btrfs or ZFS).
- An API key for a model: OpenAI Responses API (or a compatible relay) or Anthropic Messages API.

## Install

```bash
git clone https://github.com/vesten-ai/personal-agent-computer.git
cd personal-agent-computer
sudo ./install.sh
```

The script is safe to run again; it skips what is already in place. The first run builds the desktop
image, which takes about five minutes.

It creates an Incus project `pac` with:

| Piece | What it is |
|-------|------------|
| bridge `pac-br0` (10.96.10.0/24) | the agents' network. VMs can't reach each other, the host, or the internet except through the gateway |
| bridge `pac-build` (10.96.11.0/24) | used only while building the image |
| nftables table `inet pac_egress` | the host-side rule that only the gateway may leave `pac-br0` |
| image `pac-desktop/dev` | Ubuntu 24.04, XFCE, Chromium, LibreOffice, CJK fonts and input methods, the computer-use daemon |
| container `pac-gw` (10.96.10.2) | egress proxy and gateway service. Its Incus certificate is restricted to project `pac` |

Change the address ranges with `PAC_SUBNET` / `PAC_BUILD_SUBNET` if they collide with yours.

## First agent

1. Add a model key and pick the model:

   ```bash
   incus exec --project pac pac-gw -- sh -c 'echo PAC_MODEL_GPT_KEY=sk-... >> /etc/pac/gateway.env'
   incus exec --project pac pac-gw -- nano /etc/pac/gateway.toml     # see gateway/gateway.example.toml
   incus exec --project pac pac-gw -- systemctl restart pac-gateway
   ```

2. Open the page. It listens on the host's loopback only:

   ```bash
   ssh -L 8090:127.0.0.1:8090 root@your-host
   ```

   then browse to http://127.0.0.1:8090 and sign in with the admin token from
   `incus exec --project pac pac-gw -- grep PAC_ADMIN_TOKEN /etc/pac/gateway.env`.
   To publish it properly, put a TLS reverse proxy in front of 127.0.0.1:8090.

3. Add an agent, wait for its computer to start (a minute the first time), and tell it what to do.

## Credentials for the agent

Agents never hold real credentials. For each secret, the real value goes in the egress proxy's
environment and each agent gets its own placeholder:

```toml
# /etc/pac/gateway.toml in pac-gw
[secrets.github]
value_env = "PAC_SECRET_GITHUB"
vm_env = "GH_TOKEN"
hosts = ["api.github.com", "github.com"]
prefix = "ghp_"
```

```bash
incus exec --project pac pac-gw -- sh -c 'echo PAC_SECRET_GITHUB=ghp_... >> /etc/pac/secrets.env'
incus exec --project pac pac-gw -- systemctl restart pac-egress pac-gateway
```

New agents then have `$GH_TOKEN` set to a placeholder. The proxy swaps it for the real token only on
HTTPS requests to the listed hosts that come from that agent's VM, and swaps it back in responses.

## Connectors

Connectors are MCP servers the gateway runs outside the VMs; their tools appear to the model next to
computer use. One example, a notebook, is configured by default (`[connectors.notes]`). Add your own
the same way; give them credentials through `secret_env`, never through the VM.

## Sleep

An agent that is idle for 10 minutes has its VM paused (resumes instantly); after 2 hours it is shut
down (about 10 seconds to wake). The next message wakes it. Change `idle_pause_minutes` and
`idle_stop_minutes` in `gateway.toml`; 0 turns a stage off. A screen open in the browser keeps the
computer awake.

## Updating

```bash
git pull
sudo ./install.sh                    # redeploys the gateway and egress proxy
sudo image/build.sh pac-desktop/dev  # rebuild the image; new agents get it
```
