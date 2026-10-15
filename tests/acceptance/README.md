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
