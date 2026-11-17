"""Operation dispatcher shared by the CLI and the test workers.

An *op* is a plain dict, e.g.::

    {"op": "create", "tenant": "t1", "account_id": "a1", "session_id": "s1",
     "idle_ttl": 10, "hard_ttl": 100, "now": 5}
    {"op": "touch", "tenant": "t1", "session_id": "s1", "request_id": "r1",
     "now": 8}
    {"op": "scan", "tenant": "t1", "now": 50}
    {"op": "reclaim", "tenant": "t1", "request_id": "r3", "now": 60,
     "candidates": [{"session_id": "s1", "version": 1}]}
    {"op": "get", "tenant": "t1", "session_id": "s1", "now": 70}
    {"op": "watermark", "tenant": "t1"}

``now`` is the trusted clock reading for this operation; for the write
operations (create/touch/logout/reclaim) it is loaded into the injected
clock before the call, and the manager samples it inside the write
transaction.  It is never part of the idempotency fingerprint.
"""

from . import errors
from .errors import err

_CLOCK_OPS = ("create", "touch", "logout", "reclaim")


def _valid_now(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def execute_op(manager, clock, op):
    if not isinstance(op, dict):
        return err(errors.INVALID_FIELD, "op must be an object")
    name = op.get("op")
    if name in _CLOCK_OPS:
        now = op.get("now")
        if not _valid_now(now):
            return err(errors.INVALID_FIELD,
                       "now must be a non-negative integer")
        clock.set(now)
    if name == "create":
        return manager.create(op.get("tenant"), op.get("account_id"),
                              op.get("session_id"), op.get("idle_ttl"),
                              op.get("hard_ttl"), op.get("request_id"))
    if name == "touch":
        return manager.touch(op.get("tenant"), op.get("session_id"),
                             op.get("request_id"))
    if name == "logout":
        return manager.logout(op.get("tenant"), op.get("session_id"),
                              op.get("request_id"))
    if name == "scan":
        return manager.scan(op.get("tenant"), op.get("now"))
    if name == "reclaim":
        return manager.reclaim(op.get("tenant"), op.get("candidates"),
                               op.get("request_id"))
    if name == "get":
        return manager.get(op.get("tenant"), op.get("session_id"),
                           op.get("now"))
    if name == "watermark":
        return manager.watermark(op.get("tenant"))
    return err(errors.UNKNOWN_OP, f"unknown op: {name!r}")
