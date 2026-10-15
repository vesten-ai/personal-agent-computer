# Codex-owned runtime (experimental, opt-in)

The intended architecture is Codex as the root conversation, agent loop and subagent scheduler.
PAC supplies the VM, desktop operations, credential egress and human-facing UI. It does not recreate
Codex's delegation tools or implement a second model-request loop for this backend.

This first implementation adds `api = "codex"` as an optional gateway model backend. The existing
websocket/chat queue drives Codex `thread/start` / `thread/resume` and `turn/start`. Codex makes the
Responses requests and performs its own delegation; PAC answers computer and connector calls over
a private stdio MCP server. Each PAC agent persists a Codex thread ID; its rollout remains in a
separate Codex home under that agent's data directory. Existing Python/OpenAI/Anthropic agents keep
their current backend. Do not change an existing agent's backend/history in place.

## Configure a new test agent

Install a compatible Codex executable in the gateway environment first. The implementation was
validated with Codex 0.147.0; app-server is version-sensitive. Installation is not automatic.
Add a distinct model entry to `gateway.toml`, using the existing model-key environment variable:

```toml
[models.codex]
api = "codex"
model = "gpt-6.1-sol"
key_env = "PAC_MODEL_GPT_KEY"
# base_url = "https://api.openai.com/v1"
effort = "medium"
# codex_command = ["/usr/local/bin/codex", "app-server", "--listen", "stdio://"]
```

Create a **new** agent selecting `codex` in the UI, or with `pac create NAME --model codex`.
A real turn uses the configured provider and incurs its normal model usage. There is no automatic
login, reuse of the operator's Codex account, token copying or migration. The app-server child gets
a dedicated HOME/CODEX_HOME and a minimal environment including only this model's key. The selected
thread has environment access disabled on every `turn/start`, including resumed turns; host shell execution is not a PAC tool. Unsupported host
permission requests fail closed. MCP permission questions and Codex user questions use PAC's existing
human-question cards. This does not make model-requested desktop actions a hard approval boundary.

## Verified without a real model or VM

```sh
PYTHONPATH=gateway python3 tests/acceptance/check-codex.py
```

The harness starts the real installed Codex against a loopback-only canned Responses service, using
an inert key and temporary HOME. It requires no Incus or external provider. It verifies:

- Root Codex spawns a child using its built-in delegation tool and waits for its result.
- The child inherits the PAC MCP tools, requests a screenshot and receives valid PNG image content.
- The main thread receives the child's answer and produces its own response.
- A subsequent app-server process resumes the same persisted thread and completes another turn.

This takes six local provider requests, zero external model requests and zero Incus operations.
The fixture approves the MCP prompt only within the test. Production uses the human response.
Every one of the six provider requests is checked for absence of host execution, shell, patch and
image-viewer tools, covering fresh root, child and resumed root turns. Thread creation alone does not
persist this environment restriction in Codex 0.147.0.

Offline gateway tests separately cover stop/reaping, tool RPC, history-engine mismatch and questions.
When a root turn completes while a child is waiting for a person, an internal per-turn cancellation
event settles that question without an answer or Stop click. Socket handler cleanup has a bounded
wait. Tests use the real PAC worker/question path and private socket to cover natural completion,
a normal answer, explicit interruption, and a successful subsequent turn.

## Boundaries and next acceptance

Codex 0.147.0 does **not** inherit `dynamicTools` into spawned children in our probe. PAC therefore
uses inherited MCP tools rather than dynamic tools. This behavior was observed locally, not assumed
from a successful root-only test.

For this first slice, the root and its children share the PAC agent's one desktop, with serialized
tool execution. There is no per-child VM allocation, isolation, desktop ownership handoff or separate
child desktop view yet. Child tasks must finish before their root turn finishes: app-server is scoped
to one PAC turn, and background child work spanning turns is not supported. The action budget bounds
PAC computer calls, not all Codex tokens/steps. Native host tools remain unavailable. Subagent
activity appears as notices and safe metadata; a full thread tree UI is not implemented.

Before enabling this on an existing deployment, validate a **new disposable agent** with the chosen
real provider and real desktop, including approved MCP calls, task interruption and gateway restart.
That has not been run. Existing deployments and agents have not been changed. Public release remains
on hold; this backend does not resolve the separate clean-host KVM/network/desktop acceptance gate.

Protocol reference: [OpenAI Codex app-server](https://developers.openai.com/codex/app-server).
