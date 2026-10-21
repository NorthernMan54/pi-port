"""scripts/piport_service.sh against a stub pi-port (PIPORT_SERVICE_TEST): settings from the
env file, a restart on touch, a clean stop on Ctrl-C, pi-port in its own process group."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts",
                      "piport_service.sh")

STUB = textwrap.dedent("""\
    import os, signal, sys, time
    out = os.environ["STUB_OUT"]
    with open(out, "a") as f:
        f.write("start pid=%d pgid=%d args=%s mode=%s\\n" % (
            os.getpid(), os.getpgid(0), " ".join(sys.argv[1:]), os.environ.get("AIPORT_MODE")))
    def stop(sig, frame):
        with open(out, "a") as f:
            f.write("int %d\\n" % os.getpid())
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(0.2)
    """)


@unittest.skipUnless(sys.platform == "darwin", "the script uses macOS stat -f")
class TestServiceScript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "venv", "bin"))
        os.makedirs(os.path.join(repo, "piport"))
        os.symlink(sys.executable, os.path.join(repo, "venv", "bin", "python"))
        with open(os.path.join(repo, "piport", "run_all.py"), "w") as f:
            f.write(STUB)
        self.env_file = os.path.join(self.dir, "piport.env")
        with open(self.env_file, "w") as f:
            f.write("PIPORT_IFACE=en0\nPIPORT_MAC=AA\nPIPORT_DEVICE_ID=dev\n"
                    "PIPORT_STATE_DIR=/tmp/x\nAIPORT_MODE=one  # a comment\nrm -rf /\n")
        self.trigger = os.path.join(self.dir, "restart")
        self.out = os.path.join(self.dir, "stub.out")
        self.log = os.path.join(self.dir, "service.log")
        env = dict(os.environ, PIPORT_SERVICE_TEST=repo, PIPORT_ENV_FILE=self.env_file,
                   PIPORT_RESTART_FILE=self.trigger, PIPORT_LOG=self.log, STUB_OUT=self.out)
        self.proc = subprocess.Popen(["/bin/bash", SCRIPT], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()

    def lines(self, prefix):
        try:
            with open(self.out) as f:
                return [line.split() for line in f if line.startswith(prefix)]
        except FileNotFoundError:
            return []

    def wait_for(self, prefix, count, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.lines(prefix)) >= count:
                return self.lines(prefix)
            time.sleep(0.1)
        self.fail("timed out waiting for %d %r line(s); got %s" % (count, prefix, self.lines("")))

    def test_restart_and_stop(self):
        first = self.wait_for("start", 1)[0]
        pid, pgid = first[1].split("=")[1], first[2].split("=")[1]
        self.assertEqual(pid, pgid)                       # its own process group
        self.assertIn("--iface en0", " ".join(first))
        self.assertIn("mode=one", " ".join(first))        # comment stripped
        time.sleep(1.2)                                   # mtime has 1 s resolution
        with open(self.trigger, "w"):
            pass
        second = self.wait_for("start", 2)[1]
        self.assertEqual(len(self.wait_for("int", 1)), 1)  # the first was asked to stop
        self.proc.send_signal(signal.SIGINT)              # Ctrl-C
        self.proc.wait(timeout=15)
        self.assertEqual(len(self.wait_for("int", 2)), 2)
        self.assertNotEqual(second[1], first[1])
        output = self.proc.stdout.read().decode()
        self.assertIn("ignoring line", output)            # the rm line isn't run
        self.assertIn("stopping", output)
        self.assertNotIn("setpgid", output)


if __name__ == "__main__":
    unittest.main()
