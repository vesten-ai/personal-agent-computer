#!/usr/bin/env python3
"""Offline smoke from an extracted source archive; no Git, keys, Incus or network."""
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

root = Path(__file__).resolve().parents[2]
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
# Compile without writing __pycache__ into the extracted tree.
for path in root.rglob('*.py'):
    compile(path.read_bytes(), str(path.relative_to(root)), 'exec')
for path in root.rglob('*.sh'):
    subprocess.run(['bash', '-n', str(path)], cwd=root, check=True)
print('PASS: clean-directory source/config smoke; custom/default image, config preservation, Python/shell syntax')
