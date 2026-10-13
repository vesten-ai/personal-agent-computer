# Spike 1 findings: TLS interception and credential swap

**Answer: every client we tried works through the gateway's TLS interception, and the placeholder
swap reaches the real APIs.** Eight of thirteen need nothing beyond the system trust store and the
user's NSS database; the other five need one CA environment variable each, which the image sets.

Run 2026-10-13 in an agent VM (`pac-desktop/dev`, Ubuntu 24.04) whose only way out is the gateway
(spike 2). The gateway held dummy secrets, so the model APIs answered 401 with the dummy value: that
proves the request left the VM with a placeholder, was swapped on the gateway, and reached the real
server over a verified TLS connection.

## Per client

"Bare" = system store (`update-ca-certificates`) plus the agent's NSS database, no environment
variables. "Configured" = also `/etc/profile.d/pac-ca.sh` from `image/trust-gateway-ca.sh`.

| Client | Bare | Configured | Needs |
|--------|------|------------|-------|
| curl | ok | ok | |
| git | ok | ok | |
| Python `urllib` (system Python) | ok | ok | |
| pip (venv) | ok | ok | pip uses the system store through truststore |
| Chromium (xtradeb build) | ok | ok | CA in `~/.pki/nssdb` |
| gh | ok, swap reached GitHub (401 with dummy) | ok | |
| Codex CLI 0.162.1 | ok, swap reached `api.openai.com/v1/responses` (401 with dummy) | ok | |
| Claude Code (npm, native binary) | ok, swap reached `api.anthropic.com/v1/messages` (401 with dummy) | ok | |
| Python `requests` (certifi) | **fails** | ok | `REQUESTS_CA_BUNDLE` |
| Python `httpx` (certifi) | **fails** | ok | `SSL_CERT_FILE` |
| Node `fetch` | **fails** | ok | `NODE_EXTRA_CA_CERTS` (or `--use-system-ca`) |
| npm | **fails** | ok | `NODE_EXTRA_CA_CERTS` |
| uv | **fails** (bundled webpki roots) | ok | `UV_SYSTEM_CERTS` (formerly `UV_NATIVE_TLS`) |

Swap round trip against an echo service (curl, Python, Node): each client sent its placeholder, the
upstream received the real value, and the gateway scrubbed the real value out of the echoed reply, so
the client got its own placeholder back.

## From the gateway's audit log

```
api.openai.com     /v1/responses        401  swapped: openai
api.anthropic.com  /v1/messages         401  swapped: anthropic
api.github.com     /user                401  swapped: github
postman-echo.com   /headers             200  swapped: echo   scrubbed: echo
chatgpt.com        /backend-api/codex/models  403  blocked: placeholder for openai sent to chatgpt.com
```

The last line is Codex sending the OpenAI key to chatgpt.com, a host the key is not bound to. The
gateway refused to forward the placeholder there. That is the policy working as intended; in a real
deployment Codex would get a model-provider base URL and this call would not carry the key.

## Not tested yet

- **Certificate pinning.** None of these clients pinned. Apps that do (some desktop apps, mobile-style
  SDKs) will fail through interception and need a passthrough rule, which also means no swap for them.
- **Java keystores** (no JVM in the image yet).
- **OAuth flows that put credentials in request bodies** (token exchange). The addon swaps JSON and
  form bodies, but no real OAuth flow was exercised.
- **Real keys end to end.** With a real key on the gateway, Codex and Claude Code should complete
  normally; spike 3 runs with a real GPT key.
