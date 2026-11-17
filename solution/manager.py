"""Session lease manager: business rules on top of :class:`Store`.

Rules implemented here (see README for the full contract):

* Sessions are isolated by (tenant, account_id, session_id).
* ``effective_now = max(trusted_now, tenant_watermark)`` is computed and
  persisted *inside* the write transaction, after the write lock is held.
  Business rejections still commit the advanced watermark.
* A session is valid only while ``effective_now`` is strictly below both
  deadlines and the status is ``active``.
* ``touch`` only extends the idle deadline of a valid session to
  ``min(hard_deadline, max(old_idle_deadline, effective_now + idle_ttl))``;
  it never moves the hard deadline and never revives a dead session.
* ``touch`` / ``logout`` / ``reclaim`` require a ``request_id``; ``create``
  accepts an optional one.  A replayed (tenant, request_id) with identical
  parameters returns the stored result without re-sampling the clock; the
  same id with different parameters yields ``CONFLICT``.  This applies to
  successful and to business-failed results alike.
* Reclaim is two-phase: ``scan`` is read-only and never advances the
  watermark; ``reclaim`` re-reads every candidate inside a fresh write
  transaction and re-judges expiry against the *current* effective time.
"""

import json

from . import errors
from .errors import err, ok
from .store import Store

ACTIVE = "active"
LOGGED_OUT = "logged_out"
RECLAIMED = "reclaimed"


def _is_int(value):
    # bool is a subclass of int; it must not impersonate an integer.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value):
    return isinstance(value, str) and len(value) > 0


def _fingerprint(op, params):
    """Canonical fingerprint of the business parameters.

    ``now`` and ``request_id`` are deliberately excluded: the trusted clock
    is not part of the request's parameter identity.
    """
    return json.dumps({"op": op, "params": params},
                      sort_keys=True, separators=(",", ":"))


class Hooks:
    """No-op test hooks; tests subclass to inject sync points / kill windows."""

    def after_begin(self, op):
        """Called inside the write transaction, right after BEGIN."""

    def after_commit(self, op):
        """Called right after COMMIT, before the result is returned."""


class SessionManager:
    def __init__(self, db_path, clock, hooks=None):
        self._store = Store(db_path)
        self._clock = clock
        self._hooks = hooks if hooks is not None else Hooks()

    def close(self):
        self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ util
    @staticmethod
    def _check_ids(tenant, session_id=None, account_id=None):
        if not _is_nonempty_str(tenant):
            return err(errors.INVALID_FIELD, "tenant must be a non-empty string")
        if session_id is not None and not _is_nonempty_str(session_id):
            return err(errors.INVALID_FIELD,
                       "session_id must be a non-empty string")
        if account_id is not None and not _is_nonempty_str(account_id):
            return err(errors.INVALID_FIELD,
                       "account_id must be a non-empty string")
        return None

    @staticmethod
    def _check_request_id(request_id, required):
        if request_id is None:
            if required:
                return err(errors.INVALID_FIELD, "request_id is required")
            return None
        if not _is_nonempty_str(request_id):
            return err(errors.INVALID_FIELD,
                       "request_id must be a non-empty string")
        return None

    @staticmethod
    def _check_now(now):
        if not _is_int(now) or now < 0:
            return err(errors.INVALID_FIELD,
                       "now must be a non-negative integer")
        return None

    # ------------------------------------------------------- write-op skeleton
    def _write_op(self, op_name, tenant, request_id, fingerprint, business):
        """Run ``business(effective_now)`` inside a write transaction.

        Handles idempotent replay / conflict detection, clock sampling and
        watermark advancement.  Always commits (even on business rejection)
        so the advanced watermark is preserved.
        """
        store = self._store
        store.begin()
        self._hooks.after_begin(op_name)
        try:
            if request_id is not None:
                prior = store.get_request(tenant, request_id)
                if prior is not None:
                    if prior["fingerprint"] == fingerprint:
                        result = json.loads(prior["result_json"])
                        result["replayed"] = True
                    else:
                        result = err(errors.CONFLICT,
                                     "request_id reused with different"
                                     " parameters")
                    store.commit()
                    self._hooks.after_commit(op_name)
                    return result
            now = self._clock.now()
            effective_now = store.advance_watermark(tenant, now)
            result = business(effective_now)
            if request_id is not None:
                store.put_request(tenant, request_id, op_name, fingerprint,
                                  json.dumps(result, sort_keys=True))
                result = dict(result, replayed=False)
            store.commit()
        except BaseException:
            store.rollback()
            raise
        self._hooks.after_commit(op_name)
        return result

    # ------------------------------------------------------------------- ops
    def create(self, tenant, account_id, session_id, idle_ttl, hard_ttl,
               request_id=None):
        bad = (self._check_ids(tenant, session_id, account_id)
               or self._check_request_id(request_id, required=False))
        if bad:
            return bad
        if not _is_int(idle_ttl) or idle_ttl <= 0:
            return err(errors.INVALID_FIELD,
                       "idle_ttl must be a positive integer")
        if not _is_int(hard_ttl) or hard_ttl <= 0:
            return err(errors.INVALID_FIELD,
                       "hard_ttl must be a positive integer")
        fp = _fingerprint("create", {
            "account_id": account_id, "session_id": session_id,
            "idle_ttl": idle_ttl, "hard_ttl": hard_ttl,
        })

        def business(eff):
            if self._store.get_session(tenant, session_id) is not None:
                return err(errors.SESSION_EXISTS,
                           "session_id already used in this tenant")
            session = {
                "tenant": tenant, "account_id": account_id,
                "session_id": session_id, "idle_ttl": idle_ttl,
                "hard_ttl": hard_ttl, "created_at": eff,
                "idle_deadline": eff + idle_ttl,
                "hard_deadline": eff + hard_ttl,
                "status": ACTIVE, "end_reason": None,
                "version": 1, "updated_at": eff,
            }
            self._store.insert_session(session)
            return ok(session=session)

        return self._write_op("create", tenant, request_id, fp, business)

    def touch(self, tenant, session_id, request_id):
        bad = (self._check_ids(tenant, session_id)
               or self._check_request_id(request_id, required=True))
        if bad:
            return bad
        fp = _fingerprint("touch", {"session_id": session_id})

        def business(eff):
            row = self._store.get_session(tenant, session_id)
            if row is None:
                return err(errors.SESSION_NOT_FOUND, "no such session")
            if row["status"] != ACTIVE:
                return err(errors.SESSION_NOT_ACTIVE,
                           f"session is {row['status']}")
            if not (eff < row["idle_deadline"] and eff < row["hard_deadline"]):
                return err(errors.SESSION_EXPIRED, "session has expired")
            new_idle = min(row["hard_deadline"],
                           max(row["idle_deadline"], eff + row["idle_ttl"]))
            updated = dict(row, idle_deadline=new_idle,
                           version=row["version"] + 1, updated_at=eff)
            self._store.update_session(
                tenant, session_id, idle_deadline=new_idle,
                version=updated["version"], updated_at=eff)
            return ok(session=updated)

        return self._write_op("touch", tenant, request_id, fp, business)

    def logout(self, tenant, session_id, request_id):
        bad = (self._check_ids(tenant, session_id)
               or self._check_request_id(request_id, required=True))
        if bad:
            return bad
        fp = _fingerprint("logout", {"session_id": session_id})

        def business(eff):
            row = self._store.get_session(tenant, session_id)
            if row is None:
                return err(errors.SESSION_NOT_FOUND, "no such session")
            if row["status"] != ACTIVE:
                return err(errors.SESSION_NOT_ACTIVE,
                           f"session is {row['status']}")
            updated = dict(row, status=LOGGED_OUT, end_reason="logout",
                           version=row["version"] + 1, updated_at=eff)
            self._store.update_session(
                tenant, session_id, status=LOGGED_OUT, end_reason="logout",
                version=updated["version"], updated_at=eff)
            return ok(session=updated)

        return self._write_op("logout", tenant, request_id, fp, business)

    def scan(self, tenant, scan_now):
        """Read-only: list sessions that look expired at ``scan_now``.

        Never advances the watermark and never mutates state.
        """
        bad = self._check_ids(tenant) or self._check_now(scan_now)
        if bad:
            return bad
        rows = self._store.scan_expired(tenant, scan_now)
        candidates = [{
            "session_id": r["session_id"],
            "account_id": r["account_id"],
            "version": r["version"],
            "idle_deadline": r["idle_deadline"],
            "hard_deadline": r["hard_deadline"],
        } for r in rows]
        return ok(scan_now=scan_now, candidates=candidates)

    def reclaim(self, tenant, candidates, request_id):
        """Commit a reclaim batch previously produced by :meth:`scan`.

        Every candidate is re-read and re-judged against the *current*
        effective time inside this transaction; the scan snapshot is never
        trusted.
        """
        bad = (self._check_ids(tenant)
               or self._check_request_id(request_id, required=True))
        if bad:
            return bad
        if not isinstance(candidates, list):
            return err(errors.INVALID_FIELD, "candidates must be a list")
        deduped = []
        seen = set()
        for cand in candidates:
            if (not isinstance(cand, dict)
                    or not _is_nonempty_str(cand.get("session_id"))
                    or not _is_int(cand.get("version"))):
                return err(errors.INVALID_FIELD,
                           "each candidate needs a session_id string and an"
                           " integer version")
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
                row = self._store.get_session(tenant, sid)
                if row is None:
                    results.append({"session_id": sid, "outcome": "missing",
                                    "version": None})
                    continue
                if row["status"] != ACTIVE:
                    outcome = "skipped_terminated"
                elif eff < row["idle_deadline"] and eff < row["hard_deadline"]:
                    outcome = "skipped_active"
                else:
                    self._store.update_session(
                        tenant, sid, status=RECLAIMED, end_reason="expired",
                        version=row["version"] + 1, updated_at=eff)
                    row = dict(row, version=row["version"] + 1)
                    outcome = "reclaimed"
                results.append({"session_id": sid, "outcome": outcome,
                                "version": row["version"]})
            return ok(results=results, effective_now=eff)

        return self._write_op("reclaim", tenant, request_id, fp, business)

    # ------------------------------------------------------------ read-only
    def get(self, tenant, session_id, now=None):
        """Read-only session lookup; never advances the watermark."""
        bad = self._check_ids(tenant, session_id)
        if bad:
            return bad
        if now is not None:
            bad = self._check_now(now)
            if bad:
                return bad
        row = self._store.get_session(tenant, session_id)
        if row is None:
            return err(errors.SESSION_NOT_FOUND, "no such session")
        watermark = self._store.get_watermark(tenant)
        eval_now = max(watermark, now) if now is not None else watermark
        valid = (row["status"] == ACTIVE
                 and eval_now < row["idle_deadline"]
                 and eval_now < row["hard_deadline"])
        return ok(session=row, valid=valid, effective_now=eval_now,
                  watermark=watermark)

    def watermark(self, tenant):
        bad = self._check_ids(tenant)
        if bad:
            return bad
        return ok(tenant=tenant, watermark=self._store.get_watermark(tenant))
