"""Independent state machine used to compute expected results in tests.

This module deliberately does NOT import anything from ``solution``; it
re-derives the expected behaviour straight from the task statement so the
differential tests compare two independent implementations.  Only valid
inputs are modelled here (invalid-field rejection is tested separately).
"""

import json

OK = "OK"
SESSION_EXISTS = "SESSION_EXISTS"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
SESSION_EXPIRED = "SESSION_EXPIRED"
SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
CONFLICT = "CONFLICT"


def _fingerprint(op, params):
    return json.dumps({"op": op, "params": params},
                      sort_keys=True, separators=(",", ":"))


def _ok(**fields):
    result = {"ok": True, "code": OK}
    result.update(fields)
    return result


def _err(code):
    return {"ok": False, "code": code}


class Oracle:
    def __init__(self):
        self.sessions = {}     # (tenant, session_id) -> session dict
        self.watermarks = {}   # tenant -> int
        self.requests = {}     # (tenant, request_id) -> (fingerprint, result)

    # ------------------------------------------------------------- skeleton
    def _write_op(self, tenant, request_id, fingerprint, now, business):
        if request_id is not None:
            prior = self.requests.get((tenant, request_id))
            if prior is not None:
                stored_fp, stored_result = prior
                if stored_fp == fingerprint:
                    return dict(stored_result, replayed=True)
                return _err(CONFLICT)
        watermark = self.watermarks.get(tenant, 0)
        eff = now if now > watermark else watermark
        self.watermarks[tenant] = eff
        result = business(eff)
        if request_id is not None:
            self.requests[(tenant, request_id)] = (fingerprint, result)
            result = dict(result, replayed=False)
        return result

    # ------------------------------------------------------------------ ops
    def create(self, tenant, account_id, session_id, idle_ttl, hard_ttl,
               now, request_id=None):
        fp = _fingerprint("create", {
            "account_id": account_id, "session_id": session_id,
            "idle_ttl": idle_ttl, "hard_ttl": hard_ttl,
        })

        def business(eff):
            if (tenant, session_id) in self.sessions:
                return _err(SESSION_EXISTS)
            session = {
                "tenant": tenant, "account_id": account_id,
                "session_id": session_id, "idle_ttl": idle_ttl,
                "hard_ttl": hard_ttl, "created_at": eff,
                "idle_deadline": eff + idle_ttl,
                "hard_deadline": eff + hard_ttl,
                "status": "active", "end_reason": None,
                "version": 1, "updated_at": eff,
            }
            self.sessions[(tenant, session_id)] = session
            return _ok(session=dict(session))

        return self._write_op(tenant, request_id, fp, now, business)

    def touch(self, tenant, session_id, now, request_id):
        fp = _fingerprint("touch", {"session_id": session_id})

        def business(eff):
            row = self.sessions.get((tenant, session_id))
            if row is None:
                return _err(SESSION_NOT_FOUND)
            if row["status"] != "active":
                return _err(SESSION_NOT_ACTIVE)
            if not (eff < row["idle_deadline"] and eff < row["hard_deadline"]):
                return _err(SESSION_EXPIRED)
            row["idle_deadline"] = min(
                row["hard_deadline"],
                max(row["idle_deadline"], eff + row["idle_ttl"]))
            row["version"] += 1
            row["updated_at"] = eff
            return _ok(session=dict(row))

        return self._write_op(tenant, request_id, fp, now, business)

    def logout(self, tenant, session_id, now, request_id):
        fp = _fingerprint("logout", {"session_id": session_id})

        def business(eff):
            row = self.sessions.get((tenant, session_id))
            if row is None:
                return _err(SESSION_NOT_FOUND)
            if row["status"] != "active":
                return _err(SESSION_NOT_ACTIVE)
            row["status"] = "logged_out"
            row["end_reason"] = "logout"
            row["version"] += 1
            row["updated_at"] = eff
            return _ok(session=dict(row))

        return self._write_op(tenant, request_id, fp, now, business)

    def scan(self, tenant, scan_now):
        candidates = []
        for (t, _sid), row in sorted(self.sessions.items()):
            if t != tenant or row["status"] != "active":
                continue
            if scan_now >= row["idle_deadline"] or scan_now >= row["hard_deadline"]:
                candidates.append({
                    "session_id": row["session_id"],
                    "account_id": row["account_id"],
                    "version": row["version"],
                    "idle_deadline": row["idle_deadline"],
                    "hard_deadline": row["hard_deadline"],
                })
        return _ok(scan_now=scan_now, candidates=candidates)

    def reclaim(self, tenant, candidates, now, request_id):
        deduped = []
        seen = set()
        for cand in candidates:
            if cand["session_id"] not in seen:
                seen.add(cand["session_id"])
                deduped.append({"session_id": cand["session_id"],
                                "version": cand["version"]})
        fp = _fingerprint("reclaim", {
            "candidates": sorted(
                ([c["session_id"], c["version"]] for c in deduped),
                key=lambda p: p[0]),
        })

        def business(eff):
            results = []
            for cand in deduped:
                sid = cand["session_id"]
                row = self.sessions.get((tenant, sid))
                if row is None:
                    results.append({"session_id": sid, "outcome": "missing",
                                    "version": None})
                    continue
                if row["status"] != "active":
                    outcome = "skipped_terminated"
                elif eff < row["idle_deadline"] and eff < row["hard_deadline"]:
                    outcome = "skipped_active"
                else:
                    row["status"] = "reclaimed"
                    row["end_reason"] = "expired"
                    row["version"] += 1
                    row["updated_at"] = eff
                    outcome = "reclaimed"
                results.append({"session_id": sid, "outcome": outcome,
                                "version": row["version"]})
            return _ok(results=results, effective_now=eff)

        return self._write_op(tenant, request_id, fp, now, business)

    def get(self, tenant, session_id, now=None):
        row = self.sessions.get((tenant, session_id))
        if row is None:
            return _err(SESSION_NOT_FOUND)
        watermark = self.watermarks.get(tenant, 0)
        eval_now = max(watermark, now) if now is not None else watermark
        valid = (row["status"] == "active"
                 and eval_now < row["idle_deadline"]
                 and eval_now < row["hard_deadline"])
        return _ok(session=dict(row), valid=valid, effective_now=eval_now,
                   watermark=watermark)

    def watermark(self, tenant):
        return _ok(tenant=tenant, watermark=self.watermarks.get(tenant, 0))


def execute(oracle, op):
    """Mirror of solution.runner.execute_op, driving an Oracle instead."""
    name = op["op"]
    now = op.get("now")
    if name == "create":
        return oracle.create(op["tenant"], op["account_id"], op["session_id"],
                             op["idle_ttl"], op["hard_ttl"], now,
                             op.get("request_id"))
    if name == "touch":
        return oracle.touch(op["tenant"], op["session_id"], now,
                            op["request_id"])
    if name == "logout":
        return oracle.logout(op["tenant"], op["session_id"], now,
                             op["request_id"])
    if name == "scan":
        return oracle.scan(op["tenant"], now)
    if name == "reclaim":
        return oracle.reclaim(op["tenant"], op["candidates"], now,
                              op["request_id"])
    if name == "get":
        return oracle.get(op["tenant"], op["session_id"], now)
    if name == "watermark":
        return oracle.watermark(op["tenant"])
    raise AssertionError(f"oracle does not model op {name!r}")


def normalize(result):
    """Strip free-text error messages so two implementations can be compared."""
    if isinstance(result, dict):
        return {k: normalize(v) for k, v in result.items() if k != "error"}
    if isinstance(result, list):
        return [normalize(v) for v in result]
    return result
