"""The same small SQL journal runs on local SQLite and on Cloudflare D1."""

import json
import time
import uuid

from .common import SyncError

# Archive files remain independent; forwarding without an archive relies on this journal for dedup.
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS activities (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, revision TEXT NOT NULL,
        filename TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0, targets TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS deliveries (
        activity_id TEXT NOT NULL, destination TEXT NOT NULL, account TEXT NOT NULL,
        state TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '',
        remote_ids TEXT NOT NULL DEFAULT '[]',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', PRIMARY KEY(activity_id, destination, account))""",
    "CREATE TABLE IF NOT EXISTS lease (name TEXT PRIMARY KEY, owner TEXT, expires REAL)",
)


class State:
    def __init__(self, database):
        self.db = database
        self.owner = uuid.uuid4().hex

    async def initialize(self):
        for statement in SCHEMA:
            await self.db.execute(statement)

    async def acquire(self):
        rows = await self.db.execute(
            """INSERT INTO lease VALUES ('sync', ?, ?) ON CONFLICT(name) DO UPDATE SET
            owner=excluded.owner, expires=excluded.expires
            WHERE lease.expires < ? RETURNING owner""",
            (self.owner, time.time() + 300, time.time()),
        )
        if not rows:
            raise SyncError("another run is using this archive; try again after it finishes")

    async def renew(self):
        # A slow or suspended process may resume after its lease was taken: fence it before writes.
        rows = await self.db.execute(
            "UPDATE lease SET expires=? WHERE name='sync' AND owner=? RETURNING owner",
            (time.time() + 300, self.owner),
        )
        if not rows:
            raise SyncError("archive lock was lost; stop this run before retrying")

    async def release(self):
        await self.db.execute("DELETE FROM lease WHERE name='sync' AND owner=?", (self.owner,))

    async def get(self, key, default=None):
        rows = await self.db.execute("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    async def set(self, key, value):
        await self.db.execute(
            "INSERT INTO settings VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    async def discover(self, identifier, day, revision, targets):
        # Do not retroactively enable history delivery when a sync rediscovers an archived ID.
        await self.db.execute(
            "INSERT OR IGNORE INTO activities(id, day, revision, targets) VALUES (?, ?, ?, ?)",
            (identifier, day, revision, json.dumps(targets)),
        )

    async def activity(self, identifier):
        rows = await self.db.execute("SELECT * FROM activities WHERE id=?", (identifier,))
        return rows[0] if rows else None

    async def bind_owner(self, owner):
        # Without R2 there is no archive marker, so D1 must also prevent account mixing.
        existing = await self.get("garmin_owner")
        if existing is not None and existing != owner:
            raise SyncError("sync progress belongs to another Garmin account")
        if existing is None:
            await self.set("garmin_owner", owner)

    async def archived(self, identifier, record, persistent=True):
        # A ready recording has metadata only; status must not claim a durable archive exists.
        await self.db.execute(
            """UPDATE activities SET filename=?, sha256=?, size=?, revision=?, status=?,
            error='' WHERE id=?""",
            (
                record["filename"],
                record["sha256"],
                record["size"],
                record["revision"],
                "archived" if persistent else "ready",
                identifier,
            ),
        )

    async def delivery(self, identifier, destination, account):
        rows = await self.db.execute(
            "SELECT * FROM deliveries WHERE activity_id=? AND destination=? AND account=?",
            (identifier, destination, account),
        )
        return rows[0] if rows else None

    async def save_delivery(
        self, identifier, destination, account, delivery, attempts, next_attempt=0
    ):
        await self.db.execute(
            """INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(activity_id, destination, account) DO UPDATE SET
            state=excluded.state, receipt=excluded.receipt, remote_ids=excluded.remote_ids,
            attempts=excluded.attempts, next_attempt=excluded.next_attempt, error=excluded.error""",
            (
                identifier,
                destination,
                account,
                delivery.state,
                delivery.receipt,
                json.dumps(delivery.remote_ids),
                attempts,
                next_attempt,
                delivery.message,
            ),
        )

    async def summary(self):
        return {
            "activities": await self.db.execute(
                "SELECT status, COUNT(*) AS count FROM activities GROUP BY status"
            ),
            "deliveries": await self.db.execute(
                "SELECT destination, state, COUNT(*) AS count FROM deliveries "
                "GROUP BY destination,state"
            ),
            "last_run": await self.get("last_run"),
            "errors": await self.db.execute(
                "SELECT activity_id, destination, error FROM deliveries "
                "WHERE state IN ('retry','failed','needs_review') LIMIT 20"
            ),
        }
