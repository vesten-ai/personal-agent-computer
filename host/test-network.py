import importlib.util
import subprocess
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('network_preflight', Path(__file__).with_name('check-network.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NetworkPreflight(unittest.TestCase):
    def test_invalid_and_overlapping_prefixes_never_contact_host(self):
        def fail(*args, **kwargs):
            self.fail('must validate before contacting Incus')
        for agent, build in [('10.1.256', '10.2.3'), ('10.1.01', '10.2.3'),
                             ('10.1.2;id', '10.2.3'), ('10.1.2', '10.1.2')]:
            with self.assertRaises(ValueError):
                module.check(agent, build, fail)

    def test_existing_bridge_mismatch_is_refused(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout='10.96.10.1/24' if 'get' in args else 'config: {}')
        with self.assertRaisesRegex(ValueError, 'automatic subnet migration'):
            module.check('10.88.10', '10.88.11', run)

    def test_new_custom_bridges(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if 'show' in args else 0, stdout='incusbr0\n')
        module.check('10.88.10', '10.88.11', run)

    def test_incus_failure_is_not_treated_as_missing_bridge(self):
        def run(args, **kwargs):
            if kwargs.get('check'):
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 1, stdout='')
        with self.assertRaises(subprocess.CalledProcessError):
            module.check('10.88.10', '10.88.11', run)


if __name__ == '__main__':
    unittest.main()
