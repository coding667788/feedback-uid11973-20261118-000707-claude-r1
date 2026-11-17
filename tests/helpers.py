"""Shared helpers for the test-suite: per-test directories and workers."""

import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = Path(__file__).resolve().parent / "tmp"
WORKER = Path(__file__).resolve().parent / "worker.py"


class LeaseTestCase(unittest.TestCase):
    """Base class giving each test a private directory under tests/tmp."""

    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.dir = TMP_ROOT / f"{self.__class__.__name__}-{self._testMethodName}"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.db_path = self.dir / "lease.db"
        self.sync_dir = self.dir / "sync"
        self.sync_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    # -- sync points ---------------------------------------------------------
    def signal(self, name):
        (self.sync_dir / name).write_text("1", encoding="utf-8")

    def wait_file(self, name, timeout=30.0):
        path = self.sync_dir / name
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for sync file {name!r}")
            time.sleep(0.02)
        return path

    # -- workers ---------------------------------------------------------------
    def launch_worker(self, name, op, hooks=None, wait_before=None,
                      signal_after=None):
        spec = {
            "db_path": str(self.db_path),
            "sync_dir": str(self.sync_dir),
            "op": op,
            "hooks": hooks or {},
            "result_file": str(self.dir / f"{name}.result.json"),
        }
        if wait_before:
            spec["wait_before"] = wait_before
        if signal_after:
            spec["signal_after"] = signal_after
        spec_path = self.dir / f"{name}.spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), str(spec_path)], cwd=str(ROOT))
        self.addCleanup(self._kill_quietly, proc)
        return proc

    @staticmethod
    def _kill_quietly(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def join_worker(self, proc, name, timeout=30.0):
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(f"worker {name!r} did not finish in time")
        result_file = self.dir / f"{name}.result.json"
        self.assertTrue(result_file.exists(),
                        f"worker {name!r} exited with code "
                        f"{proc.returncode} and wrote no result")
        return json.loads(result_file.read_text(encoding="utf-8"))
