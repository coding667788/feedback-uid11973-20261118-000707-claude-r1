"""Unit tests: basic flows plus a randomized differential test that compares
the SQLite-backed manager against the independent in-memory oracle."""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solution import ManualClock, SessionManager, errors, execute_op
from tests.helpers import LeaseTestCase
from tests.oracle import Oracle, execute as oracle_execute, normalize


class TestBasicFlows(LeaseTestCase):
    def open_manager(self):
        clock = ManualClock()
        manager = SessionManager(self.db_path, clock)
        self.addCleanup(manager.close)
        return manager, clock

    def test_create_touch_logout_happy_path(self):
        mgr, clock = self.open_manager()
        clock.set(10)
        r = mgr.create("t1", "a1", "s1", idle_ttl=5, hard_ttl=100)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["session"]["idle_deadline"], 15)
        self.assertEqual(r["session"]["hard_deadline"], 110)
        self.assertEqual(r["session"]["version"], 1)

        clock.set(12)
        r = mgr.touch("t1", "s1", request_id="r1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["session"]["idle_deadline"], 17)
        self.assertEqual(r["session"]["hard_deadline"], 110)
        self.assertEqual(r["session"]["version"], 2)
        self.assertFalse(r["replayed"])

        clock.set(13)
        r = mgr.logout("t1", "s1", request_id="r2")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["session"]["status"], "logged_out")
        self.assertEqual(r["session"]["end_reason"], "logout")

        r = mgr.get("t1", "s1")
        self.assertTrue(r["ok"])
        self.assertFalse(r["valid"])

    def test_touch_requires_request_id(self):
        mgr, clock = self.open_manager()
        clock.set(0)
        mgr.create("t1", "a1", "s1", 5, 100)
        r = mgr.touch("t1", "s1", request_id=None)
        self.assertEqual(r["code"], errors.INVALID_FIELD)

    def test_scan_and_reclaim_flow(self):
        mgr, clock = self.open_manager()
        clock.set(0)
        mgr.create("t1", "a1", "s1", idle_ttl=10, hard_ttl=100)
        mgr.create("t1", "a1", "s2", idle_ttl=500, hard_ttl=1000)

        r = mgr.scan("t1", 50)
        self.assertTrue(r["ok"])
        self.assertEqual([c["session_id"] for c in r["candidates"]], ["s1"])
        version = r["candidates"][0]["version"]

        clock.set(60)
        r = mgr.reclaim("t1", [{"session_id": "s1", "version": version}],
                        request_id="rc1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"],
                         [{"session_id": "s1", "outcome": "reclaimed",
                           "version": 2}])

        r = mgr.get("t1", "s1")
        self.assertEqual(r["session"]["status"], "reclaimed")
        self.assertEqual(r["session"]["end_reason"], "expired")
        r = mgr.get("t1", "s2")
        self.assertEqual(r["session"]["status"], "active")


class TestDifferential(LeaseTestCase):
    """Random op sequences: manager (SQLite) must match the oracle exactly."""

    def _generate(self, seed, n_ops):
        rng = random.Random(seed)
        tenants = ["t1", "t2", "t3"]
        accounts = ["a1", "a2"]
        now = 0
        ops = []
        issued = []  # ops that carried a request_id, for replays
        for _ in range(n_ops):
            # random walk with frequent clock rollbacks
            now = max(0, now + rng.choice([-30, -7, -1, 0, 1, 3, 8, 20]))
            tenant = rng.choice(tenants)
            kind = rng.choices(
                ["create", "touch", "logout", "scan", "reclaim", "get",
                 "watermark", "replay", "conflict"],
                weights=[20, 25, 8, 10, 10, 12, 5, 6, 4])[0]
            if kind == "replay" and issued:
                op = dict(rng.choice(issued))
                op["now"] = now  # new clock reading, same parameters
            elif kind == "conflict" and issued:
                op = dict(rng.choice(issued))
                op["now"] = now
                # same request_id, different business parameters
                if op["op"] in ("touch", "logout"):
                    op["session_id"] = op["session_id"] + "-x"
                elif op["op"] == "create":
                    op["idle_ttl"] = op["idle_ttl"] + 1
                elif op["op"] == "reclaim":
                    op["candidates"] = op["candidates"] + [
                        {"session_id": "ghost", "version": 1}]
            elif kind == "create":
                op = {"op": "create", "tenant": tenant,
                      "account_id": rng.choice(accounts),
                      "session_id": f"s{rng.randrange(6)}",
                      "idle_ttl": rng.randrange(1, 30),
                      "hard_ttl": rng.randrange(1, 120), "now": now}
                if rng.random() < 0.7:
                    op["request_id"] = f"r{rng.randrange(12)}"
            elif kind in ("touch", "logout"):
                op = {"op": kind, "tenant": tenant,
                      "session_id": f"s{rng.randrange(6)}",
                      "request_id": f"r{rng.randrange(12)}", "now": now}
            elif kind == "scan":
                op = {"op": "scan", "tenant": tenant, "now": now}
            elif kind == "reclaim":
                cands = [{"session_id": f"s{rng.randrange(6)}",
                          "version": rng.randrange(1, 4)}
                         for _ in range(rng.randrange(1, 4))]
                op = {"op": "reclaim", "tenant": tenant, "candidates": cands,
                      "request_id": f"r{rng.randrange(12)}", "now": now}
            elif kind == "get":
                op = {"op": "get", "tenant": tenant,
                      "session_id": f"s{rng.randrange(6)}", "now": now}
            else:
                op = {"op": "watermark", "tenant": tenant}
            ops.append(op)
            if op.get("request_id") and op["op"] in (
                    "create", "touch", "logout", "reclaim"):
                issued.append(op)
        return ops

    def _run_seed(self, seed, n_ops):
        ops = self._generate(seed, n_ops)
        clock = ManualClock()
        manager = SessionManager(self.db_path, clock)
        self.addCleanup(manager.close)
        oracle = Oracle()
        for i, op in enumerate(ops):
            got = execute_op(manager, clock, op)
            want = oracle_execute(oracle, op)
            self.assertEqual(
                normalize(got), normalize(want),
                f"seed={seed} op#{i} = {op}\n got:  {got}\n want: {want}")

    def test_differential_seed_1(self):
        self._run_seed(1, 400)

    def test_differential_seed_2(self):
        self._run_seed(2, 400)

    def test_differential_seed_3(self):
        self._run_seed(3, 400)

    def test_differential_with_restart(self):
        """Same as above but the manager is restarted mid-sequence; the
        oracle is untouched, which also verifies persistence."""
        ops = self._generate(7, 300)
        clock = ManualClock()
        manager = SessionManager(self.db_path, clock)
        oracle = Oracle()
        for i, op in enumerate(ops):
            if i == 150:
                manager.close()
                manager = SessionManager(self.db_path, clock)
            got = execute_op(manager, clock, op)
            want = oracle_execute(oracle, op)
            self.assertEqual(
                normalize(got), normalize(want),
                f"op#{i} = {op}\n got:  {got}\n want: {want}")
        manager.close()


if __name__ == "__main__":
    unittest.main()
