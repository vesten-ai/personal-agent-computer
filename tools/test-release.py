"""Exercise source-release boundaries using an isolated disposable Git repository."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('prepare-release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd = Path.cwd()
        os.chdir(self.root)
        subprocess.run(['git', 'init', '-q'], check=True)
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid')]:
            subprocess.run(['git', 'config', key, value], check=True)
        for name in ['LICENSE', 'README.md', 'install.sh', 'host/setup.sh', 'image/provision.sh',
                     'gateway/pyproject.toml', 'gateway/pac_gateway/static/index.html',
                     'gateway/pac_gateway/static/novnc/core/rfb.js',
                     'gateway/pac_gateway/static/novnc/LICENSE.txt', 'tests/acceptance/run.sh']:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('[project]\nversion="0.1.0"\n' if name.endswith('toml') else 'fixture\n')
        (self.root / 'install.sh').chmod(0o755)
        self.commit()

    def tearDown(self):
        os.chdir(self.cwd)
        self.temp.cleanup()

    def commit(self):
        subprocess.run(['git', 'add', '.'], check=True)
        subprocess.run(['git', 'commit', '-qm', 'fixture'], check=True)

    def test_reproducible_and_ignores_working_secrets(self):
        first = release.prepare(self.root / 'a')
        (self.root / '.env').write_text('LOCAL_SECRET=must-not-ship')
        (self.root / 'README.md').write_text('uncommitted change')
        second = release.prepare(self.root / 'b')
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_rejects_tracked_runtime_data(self):
        (self.root / 'state.sqlite3').write_text('private')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'sensitive/runtime'):
            release.prepare(self.root / 'out')

    def test_rejects_personal_path(self):
        (self.root / 'README.md').write_text('/' + 'Users/example/private')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'personal path'):
            release.prepare(self.root / 'out')

    def test_requires_static_assets(self):
        (self.root / 'gateway/pac_gateway/static/novnc/core/rfb.js').unlink()
        self.commit()
        with self.assertRaisesRegex(ValueError, 'missing release files'):
            release.prepare(self.root / 'out')


if __name__ == '__main__':
    unittest.main()
