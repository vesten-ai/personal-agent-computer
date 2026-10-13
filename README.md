# Personal Agent Computer

A self-hostable computer for your AI agent. Every agent gets its own Linux
desktop virtual machine. You talk to it from a web chat page, watch its screen,
and take over the mouse and keyboard when you need to.

> Status: early. v1 runs end to end on one Incus host; expect rough edges.
> [Install it](docs/install.md).

## How it fits together

```
  browser ──► gateway (outside the VM) ──► model APIs
                 │  agent loop                (OpenAI Responses,
                 │  web chat                   Anthropic Messages)
                 │  egress proxy: swaps placeholder credentials for real ones
                 ▼
            Incus desktop VM, one per agent
              XFCE on Xvfb, Chromium, office suite, dev tools
              computer-use + browser tools exposed over MCP
              holds placeholders only, never real credentials
```

- **The agent loop runs in the gateway, not in the VM.** The VM is the agent's
  computer: a place to click, type, browse and run programs. If the VM is
  compromised or wiped, the conversation and the keys are not in it.
- **One VM per agent.** Incus runs each desktop as a full virtual machine with
  its own kernel, so agents are isolated from each other and from the host.
- **Credentials are swapped in at egress.** Programs inside the VM see
  placeholder tokens. The gateway's egress proxy replaces them with the real
  secret on the way out, for the destinations each secret is allowed to reach.
- **Two model APIs.** The OpenAI Responses API and the Anthropic Messages API.
- **Connectors over MCP.** v1 ships the MCP connector interface and no built-in
  connectors.

## v1 scope

- A plain web chat page.
- One desktop VM per agent on an Incus host.
- Watching and taking over the agent's screen from the browser.
- No messaging-app integrations and no bundled connectors.

## Repository layout

| Path | What |
|------|------|
| `install.sh` | One-shot installer for an Incus host ([docs/install.md](docs/install.md)) |
| `gateway/` | The gateway: agent loop, model adapters, web page, connectors, egress proxy addon |
| `image/` | Builds the desktop image and the computer-use daemon that runs inside it |
| `host/` | Host network and Incus setup |
| `spikes/` | The experiments that settled the design, with their findings |
| `docs/` | Install guide and design notes |

## License

[Apache License 2.0](LICENSE)
