# Changelog

## 0.1.0 (2026-10-14)

First release. One Incus host runs a gateway and one desktop VM per agent.

- **Gateway**:
  - agent loop for the OpenAI Responses API (built-in computer tool, or computer use as a function
    tool for compatible relays) and the Anthropic Messages API;
  - web chat page with the agent's live screen, take over and hand back;
  - `pac` command line.
- **Desktop image**: Ubuntu 24.04, XFCE, Chromium, LibreOffice, CJK fonts and input, and a
  computer-use daemon reached through Incus exec.
- **Credentials**:
  - VMs hold per-agent placeholders that the egress proxy swaps for real secrets on the way out,
    bound to the agent's VM address and to the secret's hosts;
  - event streams pass through as they arrive.
- **Egress**:
  - every request and DNS lookup is logged;
  - deny and allow lists cover HTTP and DNS;
  - DNS names per agent are rate-limited;
  - internal networks and the host are unreachable.
- **Connectors**: MCP servers run by the gateway, with one example.
- **Sleep**: idle computers pause, then stop with their windows saved, and wake on the next message
  with the clock corrected and memory handed back to the host.
- **Approvals**: the agent asks before paying, sending or deleting, and OpenAI safety checks go to
  the person.
- **Install**: `install.sh` sets up a fresh Incus host and is safe to re-run.

Known gaps: Claude computer use and OpenAI's built-in computer tool have been tested offline only,
and there is one admin token, no multi-user accounts.
