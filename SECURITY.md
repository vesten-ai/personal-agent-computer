# Security

Personal Agent Computer runs AI agents on full desktop computers with internet access, so its
security boundaries matter more than most projects'. These are the boundaries:

- An agent's VM cannot reach the host, the gateway's services or other agents' VMs.
- Real credentials never enter a VM. Each VM holds placeholders, which the egress proxy swaps for the
  real secret only on HTTPS requests from that VM to the hosts the secret is bound to.
- The agent loop and model keys run in the gateway, outside every VM.

A way to get around any of these is a vulnerability.

## Reporting

Please report vulnerabilities privately, through **Report a vulnerability** on this repository's
Security tab, not in a public issue. Include what you did, what happened and what you expected. We
will acknowledge the report and keep you posted on the fix.

## Known limits

These are known and documented, not vulnerabilities:

- Data can leak out slowly inside DNS names. DNS is logged and rate-limited (see docs/install.md).
- Whether an agent asks before paying, sending or deleting depends on the model. The egress rules are
  the hard limit.
- The web page has one admin token and is meant to be reached through an SSH tunnel or your own
  TLS reverse proxy.
