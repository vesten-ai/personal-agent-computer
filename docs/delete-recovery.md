# Deletion recovery

A failed VM cleanup leaves the agent record and its diagnostics available for another attempt.
`DELETE /api/agents/{id}` now returns HTTP 503 with a JSON error when that record is retained:

```json
{
  "error": "Deletion did not complete. The agent record is retained; retry deletion.",
  "code": "agent_delete_incomplete",
  "retryable": true,
  "agent_retained": true
}
```

This confirms retention of the agent record, not the VM: Incus may have removed the VM before its
connection failed. Retry deletes any remaining VM and then completes record/policy cleanup.
The CLI exits nonzero and prints the quoted `pac delete NAME` command to retry. The web page keeps
the agent selected and offers **Retry delete**; it only reports success after HTTP 200.

If cleanup fails after the record was removed (for example while rewriting egress policy), HTTP 500
has the same error code but `retryable: false` and `agent_retained: false`. Clients refresh the list
instead of promising the agent is still present. Error responses do not include manager stderr,
exception text or credentials. Normal deletion remains `200 {"ok": true}`; unknown agents remain
`404 {"error": "not found"}`. Authentication behavior is unchanged.

Deleting a record does not prune its existing history/screenshot archive files. No background archive
cleanup is added here. This feedback applies to both existing backends and the opt-in Codex backend.

Validation uses a real ASGI app, fake manager, temporary SQLite/history/screenshot files and CLI HTTP
error parsing. A local browser fixture was also checked: the first delete failed, the record and Retry
button remained visible, then retry succeeded and the record disappeared. No model, real Incus or
production environment was involved. Real-provider/desktop and clean-host acceptance remain separate.
