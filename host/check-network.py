#!/usr/bin/env python3
"""Validate PAC's /24 prefixes and existing bridges before setup mutates the host."""
import ipaddress
import subprocess
import sys


def network(prefix):
    # Prefix notation is intentional: scripts append .1/.2 and /24.
    parts = prefix.split('.')
    if len(parts) != 3 or any(not p.isdigit() or str(int(p)) != p for p in parts):
        raise ValueError(f'invalid /24 prefix: {prefix!r}; expected e.g. 10.96.10')
    return ipaddress.IPv4Network(prefix + '.0/24')


def check(agent, build, run=subprocess.run):
    nets = [network(agent), network(build)]
    if nets[0].overlaps(nets[1]):
        raise ValueError('agent and build networks must be different /24 networks')
    for name, net in zip(('pac-br0', 'pac-build'), nets):
        found = run(['incus', 'network', 'show', name], capture_output=True, text=True)
        if found.returncode:
            # Distinguish absence from an inaccessible or broken Incus daemon.
            listing = run(['incus', 'network', 'list', '--format', 'csv', '-c', 'n'],
                          capture_output=True, text=True, check=True)
            if name in listing.stdout.splitlines():
                raise ValueError(f'cannot inspect existing network {name}')
            continue
        address = run(['incus', 'network', 'get', name, 'ipv4.address'],
                      capture_output=True, text=True, check=True).stdout.strip()
        expected = f'{net.network_address + 1}/24'
        if address != expected:
            raise ValueError(f'{name} uses {address}, requested {expected}; automatic subnet migration is not supported')


if __name__ == '__main__':
    try:
        check(*sys.argv[1:])
    except (ValueError, subprocess.CalledProcessError) as e:
        sys.exit(f'PAC network preflight failed: {e}')
