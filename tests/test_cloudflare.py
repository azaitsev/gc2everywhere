"""Model R2's atomic object+metadata writes and bounded JS streams without a cloud account."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gc2everywhere.cloudflare import D1Database, R2Archive, read_stream
from gc2everywhere.common import Delivery, SyncError
from gc2everywhere.engine import Engine
from gc2everywhere.local import LocalDatabase
from gc2everywhere.state import State


class JSValue:
    def __init__(self, value):
        self.value = value

    def to_py(self):
        return self.value


class Stream:
    def __init__(self, data):
        self.chunks = [data[:3], data[3:]]
        self.cancelled = self.released = False

    def getReader(self):
        return self

    async def read(self):
        return (
            SimpleNamespace(done=False, value=JSValue(self.chunks.pop(0)))
            if self.chunks
            else (SimpleNamespace(done=True))
        )

    async def cancel(self):
        self.cancelled = True

    def releaseLock(self):
        self.released = True


class Bucket:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    async def put(self, name, body, options=None):
        self.puts += 1
        if isinstance(body, str):
            body = body.encode()
        self.objects[name] = (body, (options or {}).get("customMetadata", {}))

    async def head(self, name):
        if name not in self.objects:
            return None
        data, metadata = self.objects[name]
        return SimpleNamespace(size=len(data), customMetadata=JSValue(metadata))

    async def get(self, name):
        if name not in self.objects:
            return None
        return SimpleNamespace(body=Stream(self.objects[name][0]))


class Source:
    downloads = 0

    async def page(self, start, end, offset, limit):
        return [{"activityId": 123, "startTimeLocal": "2026-10-10", "ownerId": 456}][
            offset : offset + limit
        ]

    async def download(self, identifier):
        self.downloads += 1
        return b"PK\x03\x04opaque-original"


class Target:
    account = "account"

    def __init__(self, name, failure=False):
        self.name, self.failure, self.calls = name, failure, 0

    async def connect(self):
        return None

    async def send(self, row, content, filename):
        self.calls += 1
        assert content == b"PK\x03\x04opaque-original" and filename == "123.zip"
        if self.failure:
            raise SyncError("offline")
        return Delivery("sent")


@pytest.mark.parametrize("initial_r2", [False, True])
def test_switching_r2_preserves_receipts_and_retries(tmp_path, initial_r2):
    async def scenario():
        db = LocalDatabase(tmp_path / "state.sqlite3")
        try:
            state = State(db)
            bucket = Bucket()
            archive = R2Archive(bucket, lambda value: value)
            source, one, two = Source(), Target("one"), Target("two", failure=True)
            before = archive if initial_r2 else None
            after = None if initial_r2 else archive
            await Engine(state, before, source, [one, two], 1, 1).run(
                "sync", "2026-10-10", "2026-10-10"
            )
            await db.execute("UPDATE deliveries SET next_attempt=0")
            two.failure = False
            await Engine(state, after, source, [one, two], 1, 1).run(
                "sync", "2026-10-10", "2026-10-10"
            )
            # Mode switches must neither lose a retry nor resend the successful destination.
            assert source.downloads == 2 and one.calls == 1 and two.calls == 2
            assert (await state.activity("123"))["status"] == (
                "ready" if initial_r2 else "archived"
            )
            assert "2026/10/123.zip" in bucket.objects
            assert bucket.puts == 2  # Removing the binding must not delete or alter its objects.
        finally:
            db.close()

    asyncio.run(scenario())


def test_r2_opt_in_does_not_backfill_until_explicit_history_import(tmp_path):
    async def scenario():
        db = LocalDatabase(tmp_path / "state.sqlite3")
        try:
            state, bucket = State(db), Bucket()
            source, target = Source(), Target("one")
            await Engine(state, None, source, [target], 1, 1).run(
                "sync", "2026-10-10", "2026-10-10"
            )
            archive = R2Archive(bucket, lambda value: value)
            await Engine(state, archive, source, [target], 1, 1).run(
                "sync", "2026-10-10", "2026-10-10"
            )
            assert source.downloads == 1 and "2026/10/123.zip" not in bucket.objects
            await Engine(state, archive, source, [], 1, 1).run(
                "import-history", "2026-10-11", "2026-10-11"
            )
            assert source.downloads == 1  # Explicit backfill is still limited to selected dates.
            await Engine(state, archive, source, [], 1, 1).run(
                "import-history", "2026-10-10", "2026-10-10"
            )
            await Engine(state, archive, source, [target], 1, 1).run(
                "sync", "2026-10-10", "2026-10-10"
            )
            assert source.downloads == 2 and target.calls == 1
            assert (await state.activity("123"))["status"] == "archived"
        finally:
            db.close()

    asyncio.run(scenario())


def test_r2_reuses_original_with_stable_revision_and_account():
    async def scenario():
        bucket = Bucket()
        archive = R2Archive(bucket, lambda value: value)
        row = {"id": "123", "day": "2026-10-10", "revision": "initial"}
        original = b"PK\x03\x04opaque-original"
        await archive.bind_owner("one")
        saved = await archive.save(row, original)
        restored = await archive.lookup({**row, "revision": "new-discovery"})
        assert restored["revision"] == "initial"
        assert await archive.read(row) == (original, "123.zip")
        assert saved["filename"] == "2026/10/123.zip" and bucket.puts == 2
        with pytest.raises(SyncError, match="another Garmin account"):
            await archive.bind_owner("two")
        # R2 stores the original archive without unpacking untrusted file entries.
        assert json.loads(bucket.objects["2026/10/123.zip"][1]["gc2e"])["id"] == "123"

    asyncio.run(scenario())


def test_stream_limit_cancels_before_accumulating_excess():
    async def scenario():
        stream = Stream(b"0123456789")
        with pytest.raises(SyncError, match="size limit"):
            await read_stream(stream, 5)
        assert stream.cancelled and stream.released

    asyncio.run(scenario())


def test_d1_preserves_bound_parameters():
    class Binding:
        def prepare(self, sql):
            self.sql = sql
            return self

        def bind(self, *parameters):
            self.parameters = parameters
            return self

        async def all(self):
            return JSValue({"results": [{"owner": "locked"}]})

    binding = Binding()
    result = asyncio.run(D1Database(binding).execute("SELECT ? AS owner", ("locked",)))
    assert result == [{"owner": "locked"}]
    assert binding.parameters == ("locked",)


def test_d1_accepts_native_sdk_dictionaries():
    class Binding:
        def prepare(self, sql):
            return self

        async def all(self):
            # Current SDK bindings already convert JS objects before handing them to our adapter.
            return {"results": [{"count": 2}]}

    assert asyncio.run(D1Database(Binding()).execute("SELECT 2 AS count")) == [{"count": 2}]
