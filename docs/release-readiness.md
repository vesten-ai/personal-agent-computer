# Release readiness

Personal Agent Computer remains an independent open-source product maintained by Softopia.
The taoz platform name does not rename this project or authorize a GitHub namespace change.

Release is on hold at the owner's request. No tag, Release, namespace transfer, or image publication
is part of this preparation. Existing vesten-ai links remain canonical until a namespace is chosen.

## Evidence and remaining checks

- Main through PR #11: GPT function-tool and Claude desktop paths tested live; CI passed.
- OpenAI native computer tool: offline coverage only. Do not repeat paid tests without a new need.
- DNS: queries logged and names rate-limited; this reduces bandwidth, not a data-loss prevention guarantee.
- Approvals depend on model behavior; they are not an enforcement boundary.
- Installation: custom-subnet fresh component setup, new desktop, HTTPS egress, stopped-gateway
  recovery and config preservation passed on a disposable nested host after iterative fixes. See
  `install-acceptance.md`. Repeat the final acceptance harness on an empty host before release.
- No prebuilt image is accepted for distribution. No VM backup/restore or concurrency capacity claim.
- Codex root/child control of a real disposable desktop passed with Codex 0.147.0 and GPT through
  the configured Responses provider, including Runtime reconstruction and Stop/recovery. See
  [the 2026-10-16 record](codex-live-acceptance.md). This does not cover deployed-service restart
  or the separate clean-host installation gate.

Before release, select the canonical namespace, verify links and private reporting availability,
run clean-host acceptance, record the exact commit and checks, and approve concrete release notes.
The package version 0.1.0 alone does not mean a release exists.

## Local source artifacts (no publication)

With Python 3.12 or newer, from the repository root:

```sh
python3 tools/prepare-release.py --ref HEAD --output /tmp/pac-release
cd /tmp/pac-release
sha256sum -c *.sha256
```

This packages the selected committed Git tree, never the working directory. The filename includes
both the gateway package version and exact commit prefix. It preserves executable bits and includes
installer scripts, tests, docs, the web UI, vendored noVNC and its license. Every archived byte is
read back against its Git blob. A per-file SHA-256 manifest records the full commit and version;
the checksum file covers both the archive and manifest. Identical commits produce identical outputs.
Use a fresh output directory; existing archives are not overwritten.

Preparation requires `CHANGELOG.md` and checks that its first numbered release matches
`gateway/pyproject.toml`; an optional `Unreleased` section is skipped. Missing notes or mismatched
versions fail before any output is created.

Tracked runtime databases, environment/key files (including `.env.*` and `secrets.env`), private-key markers, Anthropic credential markers
and personal macOS home paths fail preparation. Untracked files and local changes are excluded.
These checks are a bounded hygiene check, not proof that arbitrary secrets are absent; review the
selected tree before approving release. CI exercises the boundary checks, prepares the archive twice,
compares both outputs and verifies their checksums. It does not upload or publish artifacts.

This is a source archive, not a Python wheel or a prebuilt desktop image. Extract it and follow
`docs/install.md`; clean-host acceptance remains required using `tests/acceptance/run.sh`.
