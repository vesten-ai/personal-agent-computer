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

Before release, select the canonical namespace, verify links and private reporting availability,
run clean-host acceptance, record the exact commit and checks, and approve concrete release notes.
The package version 0.1.0 alone does not mean a release exists.
