# Contributing

Thanks for helping. Issues and pull requests are welcome; for anything larger than a bug fix, open an
issue first so we can agree on the shape before you write code.

## Layout

| Path | What |
|------|------|
| `gateway/pac_gateway/` | The gateway: agent loop, model adapters, web page, `pac` CLI, store |
| `gateway/egress/` | The egress proxy addon (mitmproxy): placeholder swap, deny lists, DNS limits, audit log |
| `image/` | Builds the desktop image and the computer-use daemon (`computerd.py`) inside it |
| `host/`, `install.sh` | Incus host setup |
| `spikes/` | The experiments behind the design. Read their FINDINGS before changing what they measured |

## Checks

CI runs these on every pull request; run them before you push:

```bash
cd gateway && python -m venv .venv && .venv/bin/pip install -e '.[egress]'
.venv/bin/python -m unittest discover -s tests                         # gateway
.venv/bin/python -m unittest discover -s egress -p 'test_*.py'         # egress addon
PATH=$PWD/.venv/bin:$PATH bash egress/test_local.sh                    # egress proxy end to end
cd .. && git ls-files '*.sh' | xargs shellcheck -S warning
```

Changes to the VM image, sleep and wake, or the network rules need an Incus host with KVM to test;
say in the pull request what you ran and on what.

## Ground rules

- **Real credentials never enter an agent's VM.** Anything that would put a secret in the VM, or let
  a VM reach the gateway's own services, the host or another VM, will not be merged.
- Agents should ask before paying, sending or deleting; keep that path working.
- Keep dependencies few. The CLI uses only the standard library.

By contributing you agree that your contributions are licensed under the Apache License 2.0.
