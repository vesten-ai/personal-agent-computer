#!/usr/bin/env python3
"""No model calls: inspect a fresh local install, create one desktop, check it, delete it."""
import json
import subprocess
import time
import urllib.request


def incus(*args):
    return subprocess.check_output(['incus', 'exec', '--project', 'pac', 'pac-gw', '--', *args], text=True)


token = incus('sh', '-c', "sed -n 's/^PAC_ADMIN_TOKEN=//p' /etc/pac/gateway.env").strip()


def request(path, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request('http://127.0.0.1:8090' + path, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()


# Dummy key is used only to construct the adapter; send no messages.
incus('sh', '-c', "grep -q '^PAC_MODEL_GPT_KEY=' /etc/pac/gateway.env || "
      "printf 'PAC_MODEL_GPT_KEY=pac-install-dummy-not-a-real-key\n' >> /etc/pac/gateway.env")
incus('systemctl', 'restart', 'pac-gateway')
for _ in range(30):
    try:
        request('/api/agents')
        break
    except OSError:
        time.sleep(1)
else:
    raise RuntimeError('gateway did not start after dummy key configuration')

agent = json.loads(request('/api/agents', 'POST', {'name': 'install-acceptance'}))
try:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        agents = json.loads(request('/api/agents'))['agents']
        state = next(a['status'] for a in agents if a['id'] == agent['id'])
        if state == 'ready':
            break
        if state == 'error':
            raise RuntimeError(request(f"/api/agents/{agent['id']}/events").decode())
        time.sleep(3)
    else:
        raise RuntimeError('desktop provisioning timed out')
    vm = 'pac-' + agent['id']
    subprocess.run(['incus', 'exec', '--project', 'pac', vm, '--', 'runuser', '-u', 'agent', '--',
                    'env', 'DISPLAY=:1', 'xdpyinfo'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['incus', 'exec', '--project', 'pac', vm, '--', 'test', '-s',
                    '/usr/local/share/ca-certificates/pac-gateway.crt'], check=True)
    subprocess.run(['incus', 'exec', '--project', 'pac', vm, '--', 'curl', '-fsS', '--max-time', '20',
                    'https://example.com'], check=True, stdout=subprocess.DEVNULL)
    print('PASS: local HTTP API, fresh desktop, gateway CA, HTTPS egress; no model requests')
finally:
    request('/api/agents/' + agent['id'], 'DELETE')
