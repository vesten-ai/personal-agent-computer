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

The script can be run again; it preserves the existing gateway configuration and credentials.
It starts a stopped gateway, waits for network readiness, and fails if the gateway HTTP service does
not become ready. Re-running on an existing host has been tested; a clean-host acceptance run is
still required before claiming fresh installations are verified. The first run builds the desktop
image, which takes about five minutes.

It creates an Incus project `pac` with:

| Piece | What it is |
|-------|------------|
| bridge `pac-br0` (10.96.10.0/24) | the agents' network. VMs can't reach each other, the host, or the internet except through the gateway |
| bridge `pac-build` (10.96.11.0/24) | used only while building the image |
| nftables table `inet pac_egress` | the host-side rule that only the gateway may leave `pac-br0` |
| image `pac-desktop/dev` | Ubuntu 24.04, XFCE, Chromium, LibreOffice, CJK fonts and input methods, the computer-use daemon |
| container `pac-gw` (10.96.10.2) | egress proxy and gateway service. Its Incus certificate is restricted to project `pac` |

The default ranges must be available. Custom subnets are not yet supported end to end: some
gateway configuration still assumes 10.96.10.0/24. Do not change only the host subnet variables.

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

## From the terminal

`install.sh` also puts a `pac` command on the host, which runs in the gateway container:

```bash
pac create assistant                  # waits until its computer is ready
pac send assistant "Find the cheapest flight from Berlin to Lisbon next Friday"
pac list
pac log assistant -n 20
pac pause assistant                   # or: pac stop, pac wake
pac delete assistant
```

`pac send` follows the agent until it is done and asks you on the terminal when the agent needs an
approval; Ctrl-C asks it to stop. From another machine, set `PAC_URL` (for example the SSH tunnel's
http://127.0.0.1:8090) and `PAC_TOKEN` (the admin token) and run `pac` from `pip install ./gateway`.

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

## What leaves an agent's computer

Every connection from an agent VM goes through the egress proxy in `pac-gw`, and every request and DNS
lookup is written to `/var/log/pac/egress.jsonl` there, one JSON line each. The proxy's starting
rules are in `/etc/pac/egress-base.json`:

- `deny_hosts` and `deny_networks` refuse destinations; the host's own addresses are denied at install.
- `"default": "deny"` with `allow_hosts` lets agents reach only the listed hosts, for HTTP and DNS alike.
- `dns.names_per_minute` (300 by default) caps how many different names one agent can look up in a
  minute, and `dns.max_label` (off by default) refuses names with a longer label. Data can be smuggled
  out inside DNS names; these limits and the log make that slow and visible, not impossible. Five busy
  news sites in a row looked up 61 names; the longest label was 18 characters. Long labels do occur
  legitimately, in S3 bucket hostnames for example, which is why `max_label` is off.

The gateway keeps the file's other settings and adds the agents' placeholders to the live policy.

## Connectors

Connectors are MCP servers the gateway runs outside the VMs; their tools appear to the model next to
computer use. One example, a notebook, is configured by default (`[connectors.notes]`). Add your own
the same way; give them credentials through `secret_env`, never through the VM.

## Sleep

An agent that is idle for 10 minutes has its VM paused (resumes in a few seconds, memory stays in use).
After 2 hours the VM is stopped with its memory saved to disk, so it uses no memory. It wakes in 10 to
15 seconds with the same windows open; if the saved state can't be restored, it boots fresh. The next
message wakes it. A screen open in the browser keeps the computer awake.

Change `idle_pause_minutes` and `idle_stop_minutes` in `gateway.toml`; 0 turns a stage off. With
`idle_stop_stateful = false` the second stage is a plain shutdown instead, which is faster (about 10
seconds to wake) but closes every window. A saved VM takes up to its memory size (4 GiB by default) on
disk.

## Updating

```bash
git pull
sudo ./install.sh                    # redeploys the gateway and egress proxy
sudo image/build.sh pac-desktop/dev  # rebuild the image; new agents get it
```
