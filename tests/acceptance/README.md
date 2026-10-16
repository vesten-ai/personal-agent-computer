# Clean-host installation acceptance

Run on a disposable Ubuntu 24.04 host with KVM, Incus 6.x initialized, and an empty PAC project.
This runs the real installer and builds its desktop image. It does not use real credentials or
call model APIs. Allow at least 12 GiB RAM and 60 GiB disk when testing nested virtualization.

```bash
sudo env PAC_SUBNET=10.88.10 PAC_BUILD_SUBNET=10.88.11 bash tests/acceptance/run.sh
```

The check installs a dummy GPT key only if no GPT key is configured, creates one named test agent,
checks X11, the CA and HTTPS egress, then deletes only that agent. Run only on a disposable host.

For recovery acceptance, record checksums of gateway.env and gateway.toml inside pac-gw, stop the
container, rerun the installer with the same subnet values, and verify both checksums are unchanged.
Check the gateway API again. Also try a different requested subnet and confirm preflight refuses it
without changing the existing bridges.

This is installation/desktop validation, not live model or native OpenAI tool acceptance.


## Offline extracted-source checks

With Python 3.12+ on Linux/macOS, from an extracted source archive (no Git checkout, Codex, Incus,
real credentials or dependency installation needed):

```sh
python3 tests/acceptance/check-source.py
python3 tests/acceptance/test-source.py -v
```

The first checks required files, configuration preservation, Python/shell syntax, CLI/package version
consistency and a real stdio MCP handshake/list/screenshot through a private Unix socket to a fake
desktop. The second checks that omitted entrypoints and an inert but syntactically valid frontend
fail validation. It uses source-only disposable fixtures and makes no model or Incus calls.
These checks prepare source artifacts; they do not replace the real-host command above.

## Live Codex and disposable desktop

This is an explicit, billable acceptance test, separate from CI and from clean-host installation.
It needs Python 3.12+, Codex 0.147.0, an initialized Incus PAC project with the desktop image and
working egress, the gateway's **public CA certificate**, and a configured model key in the caller's
environment. It creates and removes only one new desktop VM. Use a config with a `default_model`
whose `api` is `codex`, plus the intended `[incus]` connection and `egress_ca_file`.

```sh
python3 tests/acceptance/check-codex-live.py \
  --config /path/to/live-test.toml --output /path/to/new-private-evidence-directory
```

All mutable runtime state and the test policy are redirected into the new output directory;
configured connectors, placeholder credentials and idle timers are disabled for this test.
The existing gateway service/configuration and its agents are not changed. The harness allows
the exact PAC MCP access prompt for its authorized synthetic task; unexpected human questions
interrupt the run without an answer. Production approval behavior is unchanged.

Acceptance requires Codex to spawn a child, have it write a random nonce through the real desktop,
wait for it, and have the root take a fresh screenshot. The harness independently reads the file
inside that VM. It then reconstructs PAC's Runtime from disk, resumes the same Codex thread,
interrupts a pending question, and verifies a subsequent screenshot turn succeeds. JSON receipts,
events, a screenshot and private Codex rollouts remain locally; do not publish raw runtime data.
The test does not restart the deployed systemd service, install Codex on a server, or prove
per-child desktop isolation, browser compatibility, clean-host installation or production readiness.
