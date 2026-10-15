"""Negative release-smoke cases using source-only temporary fixtures; stdlib only."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class SourceSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pac-source-negative-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        package = self.root / 'gateway/pac_gateway'
        # Only known package source/static assets; never copy a checkout's env/data/key files.
        shutil.copytree(ROOT / 'gateway/pac_gateway', package,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for relative in ['gateway/pyproject.toml', 'gateway/gateway.example.toml',
                         'gateway/deploy/write-config.py', 'tests/acceptance/check-source.py']:
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)

    def run_smoke(self):
        return subprocess.run([sys.executable, str(self.root / 'tests/acceptance/check-source.py')],
            cwd=self.temp.name, text=True, capture_output=True, timeout=15)

    def test_source_fixture_without_git_or_installed_package_passes(self):
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('MCP stdio/socket/screenshot', result.stdout)
        self.assertFalse((self.root / '.git').exists())

    def test_missing_codex_entrypoints_are_rejected(self):
        for relative in ['gateway/pac_gateway/codex_tools.py', 'gateway/pac_gateway/adapters/codex.py']:
            with self.subTest(path=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.unlink()
                try:
                    result = self.run_smoke()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('missing required source file: ' + relative, result.stderr)
                    self.assertNotIn('PASS:', result.stdout)
                finally:
                    path.write_bytes(original)

    def test_syntactically_valid_but_inert_frontend_is_rejected(self):
        (self.root / 'gateway/pac_gateway/codex_tools.py').write_text('raise SystemExit(0)\n')
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('MCP replies missing or out of order', result.stderr)
        self.assertNotIn('PASS:', result.stdout)


if __name__ == '__main__':
    unittest.main()
