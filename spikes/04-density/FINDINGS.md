# Spike 4 findings: memory, wake time and density

**Answer: an idle desktop VM with Chromium open costs about 1.45 GiB of host memory, a cold boot
reaches a usable desktop in 11 s, and a stateful wake brings the exact session back in about 16 s.
By memory, one 64 GiB host holds roughly 35 idle agent desktops; CPU runs out first once they work.**

Run 2026-10-13 on factory1 (Intel i7-6700, 4 cores / 8 threads, 64 GiB, btrfs pool, Incus 6.0),
3 VMs of 2 vCPU / 4 GiB each, image `pac-desktop/dev`, other tenants' VMs running alongside.

## Memory

| VM | Host RSS | Host PSS | Guest used / available |
|----|----------|----------|------------------------|
| pac-d1 | 1486 MiB | 1462 MiB | 705 / 3204 MiB |
| pac-d2 | 1460 MiB | 1436 MiB | 719 / 3189 MiB |
| pac-d3 | 1415 MiB | 1392 MiB | 708 / 3200 MiB |

The host's available memory fell by 1442 MiB per VM. KSM is on but merged only about 32 MiB across
the three in the first minutes, so there's little to count on from it yet.

Guest "used" is ~700 MiB; the rest of host RSS is guest page cache and memory the guest touched while
booting. **Incus 6.0 attaches a virtio balloon but doesn't turn on free page reporting**, so memory a
guest frees is not handed back to the host. Turning it on (`raw.qemu.conf`) is the next experiment:
it should pull idle VMs toward their ~700 MiB working set.

## Wake and boot

| Operation | Time | What survives | Memory while asleep |
|-----------|------|---------------|---------------------|
| First boot from image (cloud-init) | agent 17-19 s, desktop 22-25 s | n/a | n/a |
| Cold stop / cold boot | stop 1.1 s; agent 7.0 s, desktop 11.0 s | disk only (browser profile, files) | none |
| Stateful stop / wake | stop 22.8 s; start 12.8 s, agent 15.6 s | **everything**: same Chromium process, open windows | none (RAM written to the root volume) |
| Pause / resume | 0.0 s / 0.1 s | everything | **all of it stays resident** |

Two surprises:

- **A stateful wake is slower than a cold boot** (15.6 s vs 11.0 s to a responding agent), because it
  reads the whole 4 GiB memory image back from disk. It is still the only cheap way to keep open apps.
- **After a stateful wake the VM holds its full memory limit** (host RSS 4159 MiB vs 1486 MiB before
  the stop): restoring touches every page. Free page reporting or a balloon shrink after wake is needed
  before stateful sleep can be the default.

Stateful stop needs `migration.stateful=true` and a root disk `size.state` at least as large as the
VM's memory.

## Density estimate

- **By memory:** (64 GiB - ~8 GiB for host and gateway) / 1.45 GiB = about **35-38 idle desktops** per
  host like factory1. Sleeping VMs (cold or stateful stop) cost no RAM at all.
- **By CPU:** an i7-6700 has 8 threads. A working agent keeps Chromium and the model loop busy in
  bursts; a few concurrently active desktops per core is a reasonable first guess, so **CPU, not
  memory, caps concurrent active agents** on this class of host. Measuring that needs spike 3's loop
  running on several VMs at once.

## Suggested v1 policy

1. Idle for minutes: **pause** (instant resume, memory kept).
2. Idle longer: **stateful stop** if the agent has open work worth keeping, otherwise **cold stop**.
3. Wake on new message: start, then wait for pac-computerd to answer (7-16 s depending on the path);
   the chat page should say "waking the computer" during that time.
