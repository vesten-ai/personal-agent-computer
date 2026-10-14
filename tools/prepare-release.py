#!/usr/bin/env python3
"""Prepare a deterministic source release from a committed Git tree; never publishes."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib


def git(*args):
    return subprocess.check_output(['git', *args])


def prepare(output, ref='HEAD'):
    commit = git('rev-parse', '--verify', ref + '^{commit}').decode().strip()
    entries = git('ls-tree', '-r', '-z', commit).split(b'\0')
    files = {}
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode()
        path = PurePosixPath(name)
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'unsupported source entry: {name}')
        if path.is_absolute() or '..' in path.parts:
            raise ValueError(f'unsafe path: {name}')
        if any(p in {'.git', '.venv', '__pycache__', 'node_modules', 'dist', 'data'} for p in path.parts):
            raise ValueError(f'runtime/build directory tracked: {name}')
        if path.name == '.env' or path.name.endswith(('.db', '.sqlite', '.sqlite3', '.pem', '.key')) or path.name in {'auth.json', 'history.json', 'gateway.env'}:
            raise ValueError(f'sensitive/runtime file tracked: {name}')
        content = git('cat-file', 'blob', oid)
        if re.search(rb'(?:sk-ant-(?:api\d+|usr)-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|/Users/[^/\s]+/)', content):
            raise ValueError(f'credential or personal path marker in: {name}')
        files[name] = (content, int(mode, 8) & 0o777)
    required = ['LICENSE', 'README.md', 'install.sh', 'host/setup.sh', 'image/provision.sh',
                'gateway/pyproject.toml', 'gateway/pac_gateway/static/index.html',
                'gateway/pac_gateway/static/novnc/core/rfb.js',
                'gateway/pac_gateway/static/novnc/LICENSE.txt', 'tests/acceptance/run.sh']
    missing = sorted(set(required) - files.keys())
    if missing:
        raise ValueError(f'missing release files: {missing}')
    version = tomllib.loads(files['gateway/pyproject.toml'][0].decode())['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[a-z0-9.+-]*)', version):
        raise ValueError('unsafe version')
    stem = f'personal-agent-computer-{version}-{commit[:12]}'
    timestamp = int(git('show', '-s', '--format=%ct', commit))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (stem + '.tar.gz')
    if archive.exists():
        raise FileExistsError(archive)
    with archive.open('xb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for name, (content, mode) in sorted(files.items()):
                member = tarfile.TarInfo(f'{stem}/{name}')
                member.size, member.mode, member.mtime = len(content), mode, timestamp
                tar.addfile(member, io.BytesIO(content))
    # Read back every byte and executable bit, including all vendored static assets.
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        assert len(members) == len(files)
        for member in members:
            name = member.name.removeprefix(stem + '/')
            content, mode = files[name]
            assert member.isfile() and member.mode == mode
            assert tar.extractfile(member).read() == content
    manifest = {'version': version, 'commit': commit, 'files': {
        name: hashlib.sha256(content).hexdigest() for name, (content, _) in sorted(files.items())}}
    manifest_path = output / (stem + '.manifest.json')
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    checksum = output / (stem + '.sha256')
    checksum.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in [archive, manifest_path]))
    print(f'Prepared and verified {len(files)} files: {archive.name}')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', default='HEAD', help='Committed Git ref (working files are never packaged)')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.output, args.ref)
