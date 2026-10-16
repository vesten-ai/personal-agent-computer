# Codex and real desktop acceptance — 2026-10-16

The Codex backend at source revision `04273c3f1fce97a56bbcb66e5cd5270dbfb038ce`
completed a live integration run with Codex CLI 0.147.0, `gpt-6.1-sol` through the configured
Wodex Responses provider, and a newly provisioned Incus desktop. Elapsed time including creation,
four model turns, independent readback and cleanup was approximately 280 seconds.

The gateway Runtime and Codex app-server ran in a private local data directory; the desktop ran
on the existing PAC Incus test infrastructure. This was not a deployment of the new backend to
the existing gateway service. Only synthetic test content was placed on the disposable desktop.

## Observed results

| Check | Evidence |
| --- | --- |
| New real desktop | Runtime created one VM and waited for X11, XFCE and CA readiness. |
| Codex owns delegation | Root used its native spawn/wait tools to create and await one child. |
| Child inherits desktop tools | Four completed child MCP calls: screenshot, keypress and two typing actions. |
| Desktop write | Child opened a terminal and wrote a random nonce; an independent Incus exec read back the exact file bytes, including the newline. |
| Root verifies result | Root completed its own MCP screenshot and reported the visible nonce. |
| Persisted conversation | A newly constructed Runtime loaded the same root thread ID; a new app-server process recalled the nonce and took another screenshot. |
| Stop | A synthetic ask_person question was interrupted without an answer; its approval was cancelled and the session returned ready. |
| Turn after Stop | The same root thread completed another screenshot and replied `PAC_RESUMED_OK`. |
| Cleanup | The newly created VM was deleted and independently confirmed missing. The existing five stopped desktops and active gateway retained their prior states. |

The local private receipt contains six passing checkpoints. Supporting evidence includes the
delegation/restart/stop/recovery event streams, root/child thread IDs, a desktop screenshot and
private Codex rollouts. Those raw files are not committed. Completed MCP metadata independently
identifies calls from both root and child, rather than inferring delegation from a model's claim.

The opt-in [live harness](../tests/acceptance/check-codex-live.py) makes this repeatable.
CI runs only its offline cleanup-receipt tests: successful deletion, deletion failure, provisioning
still in progress, and a VM that remains after deletion. None of those CI checks use a model or VM.

## Limits

This demonstrates one root and one child sharing a serialized desktop. It does not establish
separate desktops per child, multi-child concurrency, background children across root turns,
browser compatibility, or provider-wide reliability. MCP access prompts were answered by the
test harness for the already-authorized synthetic task; normal product prompts still require
the human's answer. The Stop question itself was never approved.

Reconstructing the Runtime is not the same as restarting a deployed gateway service. Server-side
Codex installation/deployment and systemd restart acceptance remain separate work. Clean-host
installation/KVM/network acceptance also remains pending. Public tags, releases, namespace
changes and production deployment remain outside this result.
