"""Lifecycle-invariant tests for crimp (stdlib unittest, no deps).

These cover the behaviors that regress silently: pidfile atomicity, pid
recycling, ensure's singleton guarantee, restart on code/config change, and
the tar shipped to remotes. Run:  python3 -m unittest discover tests
"""
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crimp import cli as crimp  # noqa: E402


class Base(unittest.TestCase):
    """Rebind crimp's import-time state dir to a temp dir per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._saved = {k: getattr(crimp, k) for k in
                       ("CRIMP_DIR", "LOG", "PIDFILE", "PAUSED", "XAUTH",
                        "XVFB_PIDFILE", "HOSTS", "IS_MAC")}
        crimp.CRIMP_DIR = d
        crimp.LOG = d / "crimp.log"
        crimp.PIDFILE = d / "daemon.pid"
        crimp.PAUSED = d / "paused"
        crimp.XAUTH = d / "xauth"
        crimp.XVFB_PIDFILE = d / "xvfb.pid"
        crimp.HOSTS = ["testhost"]
        crimp.IS_MAC = True
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except OSError:
                pass
        for k, v in self._saved.items():
            setattr(crimp, k, v)
        self.tmp.cleanup()

    def live_daemon_stub(self):
        """A real child process whose cmdline matches 'crimp daemon-run'."""
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)  # crimp daemon-run"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p


class TestPidfile(Base):
    def test_atomic_write_under_concurrency(self):
        """Readers must never observe an empty/partial pidfile."""
        stop = time.monotonic() + 0.5
        bad = []

        def writer():
            while time.monotonic() < stop:
                crimp._write_pidfile(12345)

        def reader():
            while time.monotonic() < stop:
                try:
                    txt = crimp.PIDFILE.read_text()
                except FileNotFoundError:
                    continue
                parts = txt.split()
                if len(parts) < 2 or not parts[0].isdigit():
                    bad.append(txt)

        crimp._write_pidfile(1)
        ts = [threading.Thread(target=writer)] + \
             [threading.Thread(target=reader) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(bad, [], f"partial reads observed: {bad[:3]}")

    def test_pid_recycling_guard(self):
        """A live pid with a non-matching cmdline is not our daemon."""
        me = os.getpid()  # alive, cmdline contains 'python'
        crimp.PIDFILE.write_text(f"{me} 0\n")
        self.assertEqual(crimp._pid_alive(crimp.PIDFILE, "python"), me)
        self.assertIsNone(crimp._pid_alive(crimp.PIDFILE, "definitely-not-in-cmdline"))
        crimp.PIDFILE.write_text("garbage\n")
        self.assertIsNone(crimp._pid_alive(crimp.PIDFILE, "python"))
        crimp.PIDFILE.unlink()
        self.assertIsNone(crimp._pid_alive(crimp.PIDFILE, "python"))


class TestEnsure(Base):
    def _counting_popen(self, calls):
        """Intercept ONLY daemon-run spawns; everything else (e.g. the `ps`
        fallback inside _pid_alive, which subprocess.run drives through Popen)
        must reach the real Popen untouched."""
        real = subprocess.Popen
        outer = self

        def fake(cmd, **kw):
            if not (isinstance(cmd, list) and "daemon-run" in cmd):
                return real(cmd, **kw)
            calls.append(cmd)
            p = real([sys.executable, "-c",
                      "import time; time.sleep(30)  # crimp daemon-run"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            outer.procs.append(p)
            return p
        return fake

    def test_singleton_under_concurrent_ensure(self):
        calls = []
        with mock.patch.object(crimp.subprocess, "Popen",
                               side_effect=self._counting_popen(calls)):
            ts = [threading.Thread(target=crimp.cmd_ensure, args=([],))
                  for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(calls), 1, f"expected 1 spawn, got {len(calls)}")

    def test_restart_on_config_change(self):
        """Daemon started under an old CRIMP_HOSTS must be replaced."""
        stub = self.live_daemon_stub()
        # pidfile: correct mtime but a token for different hosts
        crimp.PIDFILE.write_text(
            f"{stub.pid} {crimp.module_mtime()} oldhosts-token\n")
        calls = []
        with mock.patch.object(crimp.subprocess, "Popen",
                               side_effect=self._counting_popen(calls)):
            crimp.cmd_ensure([])
        self.assertEqual(len(calls), 1, "config change must respawn the daemon")
        stub.wait(timeout=5)  # old daemon was killed

    def test_noop_when_current(self):
        stub = self.live_daemon_stub()
        crimp.PIDFILE.write_text(
            f"{stub.pid} {crimp.module_mtime()} {crimp._hosts_token()}\n")
        calls = []
        with mock.patch.object(crimp.subprocess, "Popen",
                               side_effect=self._counting_popen(calls)):
            crimp.cmd_ensure([])
        self.assertEqual(calls, [], "alive+current daemon must not respawn")
        self.assertIsNone(stub.poll(), "alive daemon must not be killed")


class TestEnvAndTar(Base):
    def test_env_num_falls_back_on_junk(self):
        with mock.patch.dict(os.environ, {"CRIMP_POLL": "not-a-number"}):
            self.assertEqual(crimp._env_num("CRIMP_POLL", "1", float), 1.0)
        with mock.patch.dict(os.environ, {"CRIMP_POLL": "2.5"}):
            self.assertEqual(crimp._env_num("CRIMP_POLL", "1", float), 2.5)

    def test_project_tar_excludes_pycache(self):
        root = crimp.module_path().parents[2]
        pc = root / "src" / "crimp" / "__pycache__"
        pc.mkdir(exist_ok=True)
        (pc / "junk.pyc").write_bytes(b"x")
        names = tarfile.open(fileobj=BytesIO(crimp._project_tar())).getnames()
        self.assertIn("pyproject.toml", names)
        self.assertTrue(any(n.endswith("__init__.py") for n in names))
        self.assertFalse([n for n in names if "__pycache__" in n],
                         "__pycache__ must not ship to remotes")


class TestPermissions(Base):
    def test_state_dir_private(self):
        crimp._ensure_dir(crimp.CRIMP_DIR)
        self.assertEqual(crimp.CRIMP_DIR.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()


class TestCrossPlatform(Base):
    """Capability-based roles: any OS can send (with a grabber) and receive."""

    def test_grabber_missing_linux_headless(self):
        crimp.IS_MAC = False
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.environ.pop("DISPLAY", None)
            self.assertIsNotNone(crimp._grabber_missing())

    def test_grabber_ok_linux_x11(self):
        crimp.IS_MAC = False
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch.object(crimp.shutil, "which",
                               side_effect=lambda b: f"/usr/bin/{b}"):
            self.assertIsNone(crimp._grabber_missing())

    def test_grabber_prefers_wayland(self):
        crimp.IS_MAC = False
        calls = []
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), \
             mock.patch.object(crimp.shutil, "which",
                               side_effect=lambda b: f"/usr/bin/{b}"), \
             mock.patch.object(crimp, "run",
                               side_effect=lambda cmd, **kw: (calls.append(cmd[0]),
                                   subprocess.CompletedProcess(cmd, 0, b"png", b""))[1]):
            self.assertEqual(crimp._grab_clipboard(), b"png")
        self.assertEqual(calls, ["wl-paste"])

    def test_receive_dispatches_mac(self):
        crimp.IS_MAC = True
        fake_stdin = mock.Mock()
        fake_stdin.buffer.read.return_value = b"pngbytes"
        with mock.patch.object(crimp, "_receive_mac") as rm, \
             mock.patch.object(crimp.sys, "stdin", fake_stdin):
            crimp.cmd_receive([])
        rm.assert_called_once_with(b"pngbytes")

    def test_ensure_skips_without_grabber(self):
        """No grabber on this box -> ensure must not spawn a doomed daemon."""
        crimp.IS_MAC = False
        calls = []
        with mock.patch.object(crimp, "_grabber_missing", return_value="x"), \
             mock.patch.object(crimp.subprocess, "Popen",
                               side_effect=lambda *a, **k: calls.append(a)):
            crimp.cmd_ensure([])
        self.assertEqual(calls, [])
