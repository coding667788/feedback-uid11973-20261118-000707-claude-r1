"""SQLite persistence layer.

Three tables:

* ``sessions``   -- one row per (tenant, session_id); terminal states are
                    kept (never deleted) so they remain queryable.
* ``watermarks`` -- one row per tenant with the persisted logical time
                    high-water mark.
* ``requests``   -- idempotency records: one row per (tenant, request_id)
                    with the parameter fingerprint and the stored result.

The database is opened in WAL mode with a generous busy timeout so that
several processes can share one file; correctness never relies on
in-process locks.  Write operations run inside ``BEGIN IMMEDIATE``
transactions so the write lock is taken up front and the clock/watermark
are always read *after* the lock, never before.
"""

import sqlite3

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        tenant        TEXT    NOT NULL,
        account_id    TEXT    NOT NULL,
        session_id    TEXT    NOT NULL,
        idle_ttl      INTEGER NOT NULL,
        hard_ttl      INTEGER NOT NULL,
        created_at    INTEGER NOT NULL,
        idle_deadline INTEGER NOT NULL,
        hard_deadline INTEGER NOT NULL,
        status        TEXT    NOT NULL,           -- active | logged_out | reclaimed
        end_reason    TEXT,                       -- logout | expired | NULL
        version       INTEGER NOT NULL,           -- 1 at create, +1 per mutation
        updated_at    INTEGER NOT NULL,
        PRIMARY KEY (tenant, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watermarks (
        tenant    TEXT    PRIMARY KEY,
        watermark INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        tenant      TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        op          TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY (tenant, request_id)
    )
    """,
)

SESSION_COLUMNS = (
    "tenant", "account_id", "session_id", "idle_ttl", "hard_ttl",
    "created_at", "idle_deadline", "hard_deadline", "status",
    "end_reason", "version", "updated_at",
)


class Store:
    def __init__(self, db_path):
        self._conn = sqlite3.connect(str(db_path), timeout=30.0,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=FULL")
        for stmt in SCHEMA:
            self._conn.execute(stmt)

    def close(self):
        self._conn.close()

    # -- transaction control -------------------------------------------------
    def begin(self):
        # BEGIN IMMEDIATE acquires the database write lock immediately, so
        # everything read afterwards is current at commit time.
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self._conn.execute("COMMIT")

    def rollback(self):
        self._conn.execute("ROLLBACK")

    # -- watermarks ----------------------------------------------------------
    def get_watermark(self, tenant):
        row = self._conn.execute(
            "SELECT watermark FROM watermarks WHERE tenant = ?",
            (tenant,)).fetchone()
        return row["watermark"] if row else 0

    def advance_watermark(self, tenant, now):
        """effective_now = max(now, watermark); persist and return it."""
        wm = self.get_watermark(tenant)
        effective = now if now > wm else wm
        self._conn.execute(
            "INSERT INTO watermarks (tenant, watermark) VALUES (?, ?) "
            "ON CONFLICT(tenant) DO UPDATE SET watermark = excluded.watermark",
            (tenant, effective))
        return effective

    # -- sessions ------------------------------------------------------------
    @staticmethod
    def _row_to_session(row):
        return {col: row[col] for col in SESSION_COLUMNS}

    def get_session(self, tenant, session_id):
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE tenant = ? AND session_id = ?",
            (tenant, session_id)).fetchone()
        return self._row_to_session(row) if row else None

    def insert_session(self, session):
        self._conn.execute(
            "INSERT INTO sessions (tenant, account_id, session_id, idle_ttl,"
            " hard_ttl, created_at, idle_deadline, hard_deadline, status,"
            " end_reason, version, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["tenant"], session["account_id"], session["session_id"],
             session["idle_ttl"], session["hard_ttl"], session["created_at"],
             session["idle_deadline"], session["hard_deadline"],
             session["status"], session["end_reason"], session["version"],
             session["updated_at"]))

    def update_session(self, tenant, session_id, **fields):
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [tenant, session_id]
        self._conn.execute(
            f"UPDATE sessions SET {assignments}"
            " WHERE tenant = ? AND session_id = ?", params)

    def scan_expired(self, tenant, scan_now):
        """Active sessions that look expired at scan_now (read-only)."""
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE tenant = ? AND status = 'active'"
            " AND (idle_deadline <= ? OR hard_deadline <= ?)"
            " ORDER BY session_id",
            (tenant, scan_now, scan_now)).fetchall()
        return [self._row_to_session(r) for r in rows]

    # -- idempotency records ---------------------------------------------------
    def get_request(self, tenant, request_id):
        row = self._conn.execute(
            "SELECT op, fingerprint, result_json FROM requests"
            " WHERE tenant = ? AND request_id = ?",
            (tenant, request_id)).fetchone()
        return dict(row) if row else None

    def put_request(self, tenant, request_id, op, fingerprint, result_json):
        self._conn.execute(
            "INSERT INTO requests (tenant, request_id, op, fingerprint,"
            " result_json) VALUES (?, ?, ?, ?, ?)",
            (tenant, request_id, op, fingerprint, result_json))
