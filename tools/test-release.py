"""Exercise source-release boundaries using an isolated disposable Git repository."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

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
        for name in ['LICENSE', 'README.md', 'CHANGELOG.md', 'install.sh', 'host/setup.sh', 'image/provision.sh',
                     'gateway/pyproject.toml', 'gateway/pac_gateway/static/index.html',
                     'gateway/pac_gateway/static/novnc/core/rfb.js',
                     'gateway/pac_gateway/static/novnc/LICENSE.txt', 'tests/acceptance/run.sh']:
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith('toml'):
                content = '[project]\nversion="0.1.0"\n'
            elif name == 'CHANGELOG.md':
                content = '# Changelog\n\n## Unreleased\n\n## 0.1.0 (2026-10-14)\n\nFixture release.\n'
            else:
                content = 'fixture\n'
            p.write_text(content)
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

    def test_requires_matching_latest_changelog_version(self):
        for content in ['# Changelog\n', '## 0.2.0\n\n## 0.1.0\n']:
            with self.subTest(content=content):
                (self.root / 'CHANGELOG.md').write_text(content)
                self.commit()
                with self.assertRaisesRegex(ValueError, 'changelog version'):
                    release.prepare(self.root / 'out')
                self.assertFalse((self.root / 'out').exists())

    def test_requires_changelog(self):
        (self.root / 'CHANGELOG.md').unlink()
        self.commit()
        with self.assertRaisesRegex(ValueError, 'missing release files'):
            release.prepare(self.root / 'out')

    def test_rejects_environment_variants_before_reading_blobs(self):
        for name in ['.env.production', 'secrets.env']:
            with self.subTest(name=name):
                path = self.root / name
                path.write_text('INERT_FIXTURE=value')
                self.commit()
                with mock.patch.object(release, 'git', wraps=release.git) as git:
                    with self.assertRaisesRegex(ValueError, 'sensitive/runtime'):
                        release.prepare(self.root / 'out')
                    oid = subprocess.check_output(['git', 'rev-parse', 'HEAD:' + name]).strip().decode()
                    self.assertNotIn(mock.call('cat-file', 'blob', oid), git.call_args_list)
                path.unlink()

    def test_requires_static_assets(self):
        (self.root / 'gateway/pac_gateway/static/novnc/core/rfb.js').unlink()
        self.commit()
        with self.assertRaisesRegex(ValueError, 'missing release files'):
            release.prepare(self.root / 'out')


if __name__ == '__main__':
    unittest.main()
