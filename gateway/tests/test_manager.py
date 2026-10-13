import shlex
import unittest

from pac_gateway.manager import IncusManager


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


if __name__ == "__main__":
    unittest.main()
