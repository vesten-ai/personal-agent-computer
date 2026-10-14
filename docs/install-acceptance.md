# Installation acceptance

The 2026-10-14 acceptance run uses a disposable Ubuntu 24.04 VM on factory1, with nested KVM,
Incus 6.0, directory storage, 4 vCPUs, 12 GiB memory and a 60 GiB disk. PAC uses non-default
10.88.10.0/24 and 10.88.11.0/24 networks. No production gateway configuration or real keys are copied.

Findings:

- Fresh-network preflight used `incus network list -c`, which Incus 6.0 does not support. Existing
  installations bypassed that branch. It now reads the JSON network list; tests cover that command.
- KVM and Incus alone are insufficient when QEMU is absent. The installer now checks for
  `qemu-system-x86_64` before setup, and the prerequisites list the Ubuntu VM-support packages.

- The non-cloud Ubuntu container ignores the gateway profile's cloud-init network configuration.
  It obtains a DHCP default route through itself and cannot reach package repositories. New gateway
  containers now use the cloud image variant.

The reusable procedure is in `tests/acceptance/`. It checks desktop startup and public HTTPS with
an inert key and makes no model calls. Clean-host acceptance is complete only when the final results
below are recorded; unit tests alone do not substitute for it.

## Results

After the fixes above and corrective retries, the fresh installation completed on the disposable host.
The desktop provisioning step took 5m35s; image compression and dependency installation add time.

- Created a new agent through the installed HTTP API using only an inert dummy key.
- X11 desktop ready; gateway CA installed; HTTPS to example.com succeeded through egress.
- Deleted the test agent through the API.
- Stopped pac-gw, reran installation with the same custom subnets, and verified the gateway recovered.
- SHA-256 checksums of gateway.env and gateway.toml matched before and after the rerun.
- A conflicting subnet preflight failed with the expected address mismatch before changing networks.

This validates fresh component setup after iterative fixes, not an untouched single-pass run of the
final commit. Repeat `tests/acceptance/run.sh` on another empty host before a public release.
Nested virtualization timing is not a production capacity benchmark. No real model key was used.

## Offline extracted-source smoke

CI extracts the candidate source archive into a fresh directory without `.git` and runs
`python3 tests/acceptance/check-source.py` (Python 3.12+). This checks Python/shell syntax and
initial gateway configuration from an unrelated working directory. Custom `PAC_IMAGE` values must
reach the generated Incus image setting, defaults must still work, and rerunning configuration
initialization must preserve existing operator settings byte for byte. The smoke uses only temporary
files; it makes no model, Incus, package-manager or network calls.

This caught an installer mismatch: `PAC_IMAGE` selected the image to build, but the initial gateway
configuration still selected `pac-desktop/dev`. The installer now passes that image choice to the
configuration initializer. Existing gateway configurations are deliberately preserved; if changing an
existing installation's image alias, update its `[incus].image` setting explicitly.

This offline smoke complements the real-host procedure above; it does not establish that KVM,
networking, package downloads or a desktop boot work on a new host.
