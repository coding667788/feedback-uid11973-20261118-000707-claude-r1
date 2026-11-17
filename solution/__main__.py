"""CLI entry point: ``python -m solution --input cases.json --output result.json``.

Input file schema::

    {
      "db_path": "sample.db",   // optional; a temp db is used when omitted
      "fresh": true,            // optional; delete any existing db first
      "ops": [ <op>, ... ]      // see solution/runner.py for op shapes
    }

The special op ``{"op": "restart"}`` closes and reopens the database
connection (simulating a process restart); it has no other effect.

Output file schema::

    {"results": [ <result>, ... ]}   // one result per input op, in order
"""

import argparse
import json
import os
import sys
import tempfile

from .clock import ManualClock
from .errors import ok
from .manager import SessionManager
from .runner import execute_op

_DB_SUFFIXES = ("", "-wal", "-shm")


def _remove_db(db_path):
    for suffix in _DB_SUFFIXES:
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass


def run_cases(spec):
    db_path = spec.get("db_path")
    temp_dir = None
    if db_path is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="lease-mgr-")
        db_path = os.path.join(temp_dir.name, "cases.db")
    elif spec.get("fresh"):
        _remove_db(db_path)

    clock = ManualClock()
    manager = SessionManager(db_path, clock)
    results = []
    try:
        for op in spec.get("ops", []):
            if isinstance(op, dict) and op.get("op") == "restart":
                manager.close()
                manager = SessionManager(db_path, clock)
                results.append(ok(restarted=True))
            else:
                results.append(execute_op(manager, clock, op))
    finally:
        manager.close()
        if temp_dir is not None:
            temp_dir.cleanup()
    return {"results": results}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="solution",
        description="Local authenticated-session lease manager")
    parser.add_argument("--input", required=True, help="cases JSON file")
    parser.add_argument("--output", required=True, help="result JSON file")
    args = parser.parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    output = run_cases(spec)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
