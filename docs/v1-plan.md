# v1 plan

v1 is one Incus host, a handful of agents, a plain web chat page, and the ability to watch and take
over each agent's screen. Everything here follows from the four spikes (`spikes/*/FINDINGS.md`).

## Shape

```
browser ──https──► gateway (container on the agent bridge, outside every VM)
                     ├─ web chat + screen viewer (noVNC)
                     ├─ agent loop, one session per agent
                     │    adapters: Anthropic computer toolset, OpenAI computer tool,
                     │              OpenAI function form (for relays)
                     ├─ MCP client for connectors (connectors run outside the VM)
                     ├─ computer manager ──Incus API (restricted cert, project pac)──► host
                     │                        exec → pac-computerd (stdio over vsock)
                     │                        exec → VNC relay to x11vnc on the VM's loopback
                     └─ egress proxy: placeholder → secret, per agent; audit log

Incus host: project pac, agent bridge (VMs port-isolated, IP/MAC-filtered, only the gateway
            may leave), one desktop VM per agent, /home/agent on its own volume
```

## Decisions (defaults; change them in review)

| Topic | v1 choice | Why |
|-------|-----------|-----|
| Gateway language | Python 3.12, asyncio, Starlette + uvicorn, stdlib HTTP for model calls | adapters and egress addon are already Python; mitmproxy is Python; one language for contributors |
| Web UI | plain HTML + JS, no build step; noVNC vendored | v1 is a single page; nothing to compile keeps self-hosting simple |
| Where the gateway runs | in the `pac-gw` container next to the egress proxy | it must sit on the agent bridge anyway; keeps the host clean |
| Gateway → Incus | Incus API over HTTPS on the host bridge address, with a client certificate restricted to project `pac` | the gateway can create and drive agent VMs but can't touch anything else on the host |
| Computer channel | `pac-computerd` over Incus exec (vsock), as in spike 3 | no network port in the VM; the same daemon serves every model format |
| Screen | x11vnc on the VM's loopback, relayed through Incus exec to noVNC in the page | spike image already has it; neko can replace it later |
| Takeover | page starts view-only; "Take over" pauses the agent loop and enables input; "Hand back" resumes it | one driver at a time |
| State | SQLite on the gateway: agents, sessions, append-only message log, audit | replay must be byte-exact for thinking blocks and encrypted reasoning |
| Secrets | gateway-side file, one placeholder per agent per secret, bound to hosts and to the agent's VM address | spike 1 showed the swap works for every client we tried |
| Sleep | pause after 10 min idle, cold stop after 2 h; wake on the next message | spike 4: pause is instant, cold boot 11 s; stateful stop waits for free page reporting |
| Auth | one admin token for v1 | multi-user is out of scope |
| Connectors | MCP servers configured on the gateway, offered to the model as tools | VM never sees connector credentials |

## Milestones

1. **One agent end to end.** Computer manager (create from image, trust the gateway CA, start, stop,
   exec), gateway service with one agent, chat page that sends a task and streams the loop's steps and
   screenshots back. CLI: `pac agent create|start|stop|delete`.
2. **Watch and take over.** noVNC in the page through the gateway's VNC relay; take over / hand back.
3. **Secrets and connectors.** Per-agent placeholders generated from the gateway's secret file; the
   egress policy rebuilt when agents change; MCP connector interface with one example server.
4. **Run it for real.** Sleep and wake, approvals (the agent asks before paying, sending or deleting;
   OpenAI safety checks go to the person), install script for a fresh Incus host, docs.

## Known gaps carried from the spikes

- DNS can carry data out (gateway resolves any name). Every query is logged, deny/allow lists apply,
  and distinct names per agent per minute are capped; small leaks remain possible.
- ~~Stateful wake restores the full memory limit.~~ Fixed by cycling the balloon after a restore; free
  page reporting is on (see spike 4's follow-up).
- ~~Claude computer use tested offline only.~~ Tested live on factory1 2026-10-14 (Claude Opus 5.5):
  browser, terminal, Chinese input, an approval before deleting, and a saved sleep plus gateway
  restart in the middle of a conversation. OpenAI's built-in computer tool is still offline only.
