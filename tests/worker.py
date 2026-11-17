"""Subprocess worker used by the concurrency / kill tests.

Usage:  python tests/worker.py <spec.json>

Spec schema::

    {
      "db_path": "...",            // shared database file
      "sync_dir": "...",           // directory for file-based sync points
      "op": { ... },               // one operation (see solution.runner)
      "wait_before": "file",       // optional: wait for this sync file first
      "signal_after": "file",      // optional: create this sync file at end
      "hooks": {                   // optional sync points inside the txn
        "after_begin":  {"signal": "f1", "wait": "f2"},
        "after_commit": {"signal": "f3", "wait": "f4"}
      },
      "result_file": "..."         // where the JSON result is written
    }

Sync points are plain files: a worker blocks until a ``wait`` file exists
and signals peers by creating a ``signal`` file.  This keeps the processes
fully independent (no shared in-process locks).
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from solution import Hooks, ManualClock, SessionManager, execute_op  # noqa: E402

WAIT_TIMEOUT = 60.0


def wait_for(path):
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.02)


class FileHooks(Hooks):
    def __init__(self, sync_dir, cfg):
        self._dir = sync_dir
        self._cfg = cfg or {}

    def _run(self, name):
        cfg = self._cfg.get(name)
        if not cfg:
            return
        if "signal" in cfg:
            (self._dir / cfg["signal"]).write_text("1", encoding="utf-8")
        if "wait" in cfg:
            wait_for(self._dir / cfg["wait"])

    def after_begin(self, op):
        self._run("after_begin")

    def after_commit(self, op):
        self._run("after_commit")


def main():
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    sync_dir = Path(spec["sync_dir"])
    sync_dir.mkdir(parents=True, exist_ok=True)

    if spec.get("wait_before"):
        wait_for(sync_dir / spec["wait_before"])

    hooks = FileHooks(sync_dir, spec.get("hooks"))
    clock = ManualClock()
    manager = SessionManager(spec["db_path"], clock, hooks=hooks)
    try:
        result = execute_op(manager, clock, spec["op"])
    finally:
        manager.close()

    Path(spec["result_file"]).write_text(json.dumps(result), encoding="utf-8")
    if spec.get("signal_after"):
        (sync_dir / spec["signal_after"]).write_text("1", encoding="utf-8")


if __name__ == "__main__":
    main()
