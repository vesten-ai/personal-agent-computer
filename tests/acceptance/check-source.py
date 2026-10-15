#!/usr/bin/env python3
"""Offline smoke from an extracted source archive; no Git, real keys, Incus or external network."""
from pathlib import Path
import base64
import json
import os
import subprocess
from unittest.mock import Mock
import sys
import tempfile
import tomllib

root = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
required = [
    'gateway/pyproject.toml', 'gateway/gateway.example.toml', 'gateway/deploy/write-config.py',
    'gateway/pac_gateway/cli.py', 'gateway/pac_gateway/adapters/codex.py',
    'gateway/pac_gateway/codex_tools.py', 'gateway/pac_gateway/static/index.html',
    'gateway/pac_gateway/static/novnc/core/rfb.js', 'gateway/pac_gateway/static/novnc/LICENSE.txt',
]
for relative in required:
    if not (root / relative).is_file():
        raise SystemExit(f'missing required source file: {relative}')
with tempfile.TemporaryDirectory() as temporary:
    destination = Path(temporary) / 'gateway.toml'
    command = [sys.executable, str(root / 'gateway/deploy/write-config.py'),
               str(root / 'gateway/gateway.example.toml'), str(destination), '--image']
    # Run outside the source directory to catch accidental cwd assumptions.
    for image in ['custom-desktop/candidate', 'desktop/quote"test']:
        subprocess.run([*command, image], cwd=temporary, check=True)
        config = tomllib.loads(destination.read_text())
        assert config['incus']['image'] == image
        original = destination.read_bytes()
        subprocess.run([*command, 'replacement/must-not-win'], cwd=temporary, check=True)
        assert destination.read_bytes() == original, 'reinstall overwrote existing configuration'
        assert destination.stat().st_mode & 0o777 == 0o644
        destination.unlink()
    subprocess.run([*command, 'pac-desktop/dev'], cwd=temporary, check=True)
    assert tomllib.loads(destination.read_text())['incus']['image'] == 'pac-desktop/dev'
# The MCP frontend runs as a separate script in Codex's isolated environment. Importing the
# adapter alone cannot catch a missing frontend or an incorrect script/cwd/socket setup.
sys.path.insert(0, str(root / 'gateway'))
from pac_gateway.adapters.codex import CodexComputerAgent, ToolSocket
png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
with tempfile.TemporaryDirectory(prefix='pac-source-mcp-') as temporary:
    env = {'PATH': os.environ.get('PATH', ''), 'HOME': temporary,
           'PYTHONPATH': str(root / 'gateway'), 'PYTHONDONTWRITEBYTECODE': '1'}
    version = subprocess.check_output([sys.executable, '-m', 'pac_gateway.cli', '--version'],
                                      cwd=temporary, env=env, text=True).strip()
    expected = tomllib.loads((root / 'gateway/pyproject.toml').read_text())['project']['version']
    assert version == f'pac {expected}', 'CLI and package version differ'
    adapter = CodexComputerAgent(model='fixture', api_key='inert-unused-key', system='fixture', home=temporary)
    adapter.computer = Mock()
    adapter.computer.screenshot.return_value = png
    adapter.steps, adapter.max_steps = 0, 2
    tools = ToolSocket(adapter)
    try:
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-06-18', 'capabilities': {},
                'clientInfo': {'name': 'pac-source-check', 'version': '0'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'pac_computer', 'arguments': {'type': 'screenshot'}}},
        ]
        # No PYTHONPATH, installed package, real key, Codex binary or VM in the frontend process.
        result = subprocess.run([sys.executable, str(root / 'gateway/pac_gateway/codex_tools.py')],
            cwd=temporary, env={'PATH': env['PATH'], 'HOME': temporary,
                                'PAC_CODEX_TOOL_SOCKET': tools.path},
            input=''.join(json.dumps(m) + '\n' for m in messages), text=True,
            capture_output=True, timeout=5, check=True)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        assert [r['id'] for r in replies] == [1, 2, 3], 'MCP replies missing or out of order'
        assert replies[0]['result']['serverInfo']['name'] == 'pac-computer'
        assert any(t['name'] == 'pac_computer' for t in replies[1]['result']['tools'])
        assert not replies[2]['result'].get('isError'), 'MCP tool call failed'
        image = replies[2]['result']['content'][0]
        assert image['type'] == 'image' and image['mimeType'] == 'image/png'
        assert base64.b64decode(image['data']) == png, 'MCP screenshot bytes changed'
        adapter.computer.screenshot.assert_called_once()
        assert not result.stderr, 'MCP frontend unexpectedly wrote stderr'
    finally:
        tools.close()
    assert not tools.server.active and not tools.server.connections, 'MCP socket leaked handlers or peers'
# Compile without writing __pycache__ into the extracted tree.
for path in root.rglob('*.py'):
    compile(path.read_bytes(), str(path.relative_to(root)), 'exec')
for path in root.rglob('*.sh'):
    subprocess.run(['bash', '-n', str(path)], cwd=root, check=True)
print('PASS: clean-directory source/config smoke; custom/default image, config preservation, Python/shell syntax, CLI version, MCP stdio/socket/screenshot')
