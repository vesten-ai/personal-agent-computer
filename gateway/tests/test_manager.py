import shlex
import subprocess
import unittest
from unittest import mock

from pac_gateway.manager import IncusManager, ManagerError, mib


class Argv(unittest.TestCase):
    def test_local(self):
        m = IncusManager(project="pac")
        self.assertEqual(m.argv("start", "pac-x"), ["incus", "start", "pac-x", "--project", "pac"])

    def test_remote_exec_runs_as_agent_on_display(self):
        m = IncusManager(remote="host", project="pac")
        argv = m.computerd_argv("pac-x")
        self.assertEqual(argv[:6], ["incus", "exec", "--project", "pac", "host:pac-x", "--mode"])
        self.assertIn("pac-computerd", argv)
        self.assertEqual(argv[argv.index("--") + 1:argv.index("--") + 4], ["runuser", "-u", "agent"])

    def test_over_ssh_the_remote_command_is_one_quoted_string(self):
        m = IncusManager(command=["ssh", "-i", "k", "root@h", "incus"], project="pac")
        argv = m.argv("launch", "img", "pac-x", "-c", "cloud-init.user-data=#cloud-config\n{\"a\": 1}")
        self.assertEqual(argv[:5], ["ssh", "-i", "k", "root@h", "-T"])
        remote = shlex.split(argv[5])
        self.assertEqual(remote[0], "incus")
        self.assertIn("cloud-init.user-data=#cloud-config\n{\"a\": 1}", remote)

    def test_remote_launch_takes_the_image_from_the_remote(self):
        m = IncusManager(remote="host", project="pac", image="pac-desktop/dev")
        seen = []
        m._run = lambda *args, **kw: seen.append(args) or ""
        m.create("pac-x", "CA")
        self.assertEqual(seen[0][:3], ("launch", "host:pac-desktop/dev", "host:pac-x"))

    def test_info_of_a_stopped_vm(self):
        m = IncusManager(project="pac")
        m._run = lambda *a, **k: '[{"name": "pac-x", "status": "Stopped", "state": {"network": null}}]'
        info = m.info("pac-x")
        self.assertEqual((info.state, info.address), ("Stopped", None))

    def test_a_saved_vm_that_wont_restore_boots_fresh(self):
        m = IncusManager(project="pac")
        calls = []

        def run(*args, **kw):
            calls.append(args)
            if args[0] == "list":
                return '[{"name": "pac-x", "status": "Stopped", "stateful": true, "state": {"network": null}}]'
            if args[0] == "start" and "--stateless" not in args:
                raise ManagerError("restore failed")
            return ""
        m._run = run
        self.assertEqual(m.start("pac-x"), "booted")
        self.assertEqual(calls[-1], ("start", "pac-x", "--stateless"))

    def test_reclaim_shrinks_the_balloon_and_grows_it_back(self):
        m = IncusManager(project="pac", memory="4GiB")
        sets = []
        m._run = lambda *a, **k: sets.append(a[-1]) or ""
        meminfo = subprocess.CompletedProcess([], 0, stdout="4002000\n3280000\n")  # ~705 MiB used
        with mock.patch.object(subprocess, "run", return_value=meminfo):
            m.reclaim_memory("pac-x")
        self.assertEqual(sets, ["limits.memory=1536MiB", "limits.memory=4095MiB", "limits.memory=4GiB"])

    def test_memory_sizes(self):
        self.assertEqual((mib("4GiB"), mib("1536MiB")), (4096, 1536))


if __name__ == "__main__":
    unittest.main()
