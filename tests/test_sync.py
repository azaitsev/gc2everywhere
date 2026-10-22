"""Exercise interrupted runs and delivery intent without any live credentials or sockets."""

import asyncio
import io
import json
import struct
import zipfile

import pytest

from gc2everywhere.common import Delivery, HTTPError, Response, SyncError, Transport
from gc2everywhere.destinations.effortsignal import EffortSignal
from gc2everywhere.destinations.intervals import Intervals
from gc2everywhere.engine import Engine
from gc2everywhere.files import extract_fit
from gc2everywhere.garmin import Garmin
from gc2everywhere.local import LocalArchive, LocalDatabase
from gc2everywhere.state import State

FIT = bytes([12, 16]) + struct.pack("<HI", 0, 1) + b".FIT" + b"x\x00\x00"
DAY = "2026-10-10"
RECEIPT = "12345678-1234-1234-1234-123456789abc"


def zipped(name="garmin_original.fit", content=FIT):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return output.getvalue()


class Source:
    def __init__(self):
        self.downloads = 0
        self.rows = [{"activityId": 123, "startTimeLocal": DAY, "ownerId": 456}]

    async def page(self, start, end, offset, limit):
        return self.rows[offset : offset + limit]

    async def download(self, identifier):
        self.downloads += 1
        return zipped()


class Target:
    def __init__(self, name="one", failure=None, pending=False):
        self.name, self.account = name, "account"
        self.failure, self.pending = failure, pending
        self.sent, self.polled = [], []

    async def connect(self):
        return None

    async def send(self, row, content, filename):
        self.sent.append((row["id"], row["revision"], filename))
        if self.failure:
            raise self.failure
        return Delivery("accepted", RECEIPT) if self.pending else Delivery("sent")

    async def poll(self, receipt):
        self.polled.append(receipt)
        return Delivery("sent", receipt)


@pytest.fixture
def storage(tmp_path):
    archive = LocalArchive(tmp_path / "archive")
    db = LocalDatabase(tmp_path / "state" / "journal.sqlite3")
    state = State(db)
    yield state, archive
    db.close()


def test_repeated_run_and_lost_journal_reuse_files(storage, tmp_path):
    async def scenario():
        state, archive = storage
        source, target = Source(), Target()
        await Engine(state, archive, source, [target]).run("sync", DAY, DAY)
        await Engine(state, archive, source, [target]).run("sync", DAY, DAY)
        assert source.downloads == 1
        assert len(target.sent) == 1
        assert (archive.root / "2026/10/123.fit").read_bytes() == FIT
        assert extract_fit((archive.root / "2026/10/123.zip").read_bytes()) == FIT
        # A fresh journal must recover from sidecars without silently re-sending history.
        other_db = LocalDatabase(tmp_path / "other" / "journal.sqlite3")
        try:
            other_state = State(other_db)
            await Engine(other_state, archive, source, [target]).run("sync", DAY, DAY)
            assert source.downloads == 1 and len(target.sent) == 1
        finally:
            other_db.close()

    asyncio.run(scenario())


def test_history_and_new_destination_need_explicit_send(storage):
    async def scenario():
        state, archive = storage
        source, one, two = Source(), Target("one"), Target("two")
        await Engine(state, archive, source, []).run("import-history", DAY, DAY)
        await Engine(state, archive, source, [one, two]).run("sync", DAY, DAY)
        assert not one.sent and not two.sent
        await Engine(state, archive, None, [one]).run("send", DAY, DAY)
        await Engine(state, archive, source, [one, two]).run("sync", DAY, DAY)
        assert len(one.sent) == 1 and not two.sent
        assert json.loads((await state.activity("123"))["targets"]) == ["one"]

    asyncio.run(scenario())


def test_new_key_does_not_send_old_sync_archive(storage):
    async def scenario():
        state, archive = storage
        source, one, two = Source(), Target("one"), Target("two")
        await Engine(state, archive, source, [one]).run("sync", DAY, DAY)
        await Engine(state, archive, source, [one, two]).run("sync", DAY, DAY)
        assert len(one.sent) == 1 and not two.sent

    asyncio.run(scenario())


def test_failure_isolated_and_retry_after_persists(storage):
    async def scenario():
        state, archive = storage
        source = Source()
        one, two = Target("one", HTTPError("one", 429, 3600)), Target("two")
        result = await Engine(state, archive, source, [one, two]).run("sync", DAY, DAY)
        assert result["errors"] == 1 and len(two.sent) == 1
        await Engine(state, archive, source, [one, two]).run("sync", DAY, DAY)
        assert len(one.sent) == 1 and len(two.sent) == 1
        assert (await state.delivery("123", "one", "account"))["state"] == "retry"

    asyncio.run(scenario())


def test_accepted_receipt_is_polled_without_resending(storage):
    async def scenario():
        state, archive = storage
        source, target = Source(), Target(pending=True)
        first = await Engine(state, archive, source, [target]).run("sync", DAY, DAY)
        second = await Engine(state, archive, source, [target]).run("sync", DAY, DAY)
        assert first["accepted"] == 1 and first["sent"] == 0
        assert second["sent"] == 1
        assert len(target.sent) == 1 and target.polled == [RECEIPT]

    asyncio.run(scenario())


def test_forwarding_without_archive_reuses_memory_and_receipts(storage):
    async def scenario():
        state, local = storage
        source, one, two, later = Source(), Target("one"), Target("two"), Target("later")
        first = await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        second = await Engine(state, None, source, [one, two, later], 1, 1).run("sync", DAY, DAY)
        assert first["downloaded"] == 1 and first["sent"] == 2
        assert second["downloaded"] == 0 and second["sent"] == 0
        assert source.downloads == 1 and not later.sent
        assert one.sent[0][2] == two.sent[0][2] == "123.zip"
        assert (await state.activity("123"))["status"] == "ready"
        assert not list(local.root.iterdir())  # Forwarding must not write archive files.

    asyncio.run(scenario())


def test_forwarding_retries_only_unfinished_destination(storage):
    async def scenario():
        state, _ = storage
        source, one, two = Source(), Target("one"), Target("two", SyncError("offline"))
        first = await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert first["errors"] == 1 and source.downloads == 1
        await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 1  # Respect retry timing before fetching the original again.
        await state.db.execute("UPDATE deliveries SET next_attempt=0")
        two.failure = None
        await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 2 and len(one.sent) == 1 and len(two.sent) == 2
        assert two.sent[0] == two.sent[1]  # Source revision stays stable across invocations.

    asyncio.run(scenario())


def test_forwarding_polls_receipt_without_downloading_again(storage):
    async def scenario():
        state, _ = storage
        source, target = Source(), Target(pending=True)
        await Engine(state, None, source, [target], 1, 1).run("sync", DAY, DAY)
        result = await Engine(state, None, source, [target], 1, 1).run("sync", DAY, DAY)
        assert result["sent"] == 1 and source.downloads == 1
        assert target.polled == [RECEIPT] and len(target.sent) == 1

    asyncio.run(scenario())


def test_forwarding_does_not_download_when_destination_cannot_connect(storage):
    class Offline(Target):
        async def connect(self):
            raise SyncError("offline")

    async def scenario():
        state, _ = storage
        source = Source()
        result = await Engine(state, None, source, [Offline()], 1, 1).run("sync", DAY, DAY)
        assert result["errors"] == 1 and source.downloads == 0
        assert (await state.activity("123"))["status"] == "pending"

    asyncio.run(scenario())


def test_forwarding_keeps_account_and_download_bounds(storage):
    async def scenario():
        state, _ = storage
        source, one, two = Source(), Target("one"), Target("two")
        source.rows.append({"activityId": 124, "startTimeLocal": DAY, "ownerId": 456})
        await state.initialize()
        # A backlog already in D1 must still obey the one-download budget across services.
        for identifier in ("123", "124"):
            await state.discover(identifier, DAY, "2026-10-10T00:00:00Z", ["one", "two"])
        await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 1 and len(one.sent) == len(two.sent) == 1
        source.rows[1]["ownerId"] = 789
        with pytest.raises(SyncError, match="another Garmin account"):
            await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command,targets", [("sync", []), ("import-history", []), ("send", [Target()])]
)
def test_forwarding_rejects_archive_modes_before_listing(storage, command, targets):
    state, _ = storage
    source = Source()
    with pytest.raises(SyncError, match="without R2"):
        asyncio.run(Engine(state, None, source, targets).run(command, DAY, DAY))
    assert source.downloads == 0


@pytest.mark.parametrize("status", [404, 429])
def test_forwarding_source_error_does_not_pause_destinations(storage, status):
    class BrokenSource(Source):
        async def download(self, identifier):
            self.downloads += 1
            raise HTTPError("Garmin", status, 300)

    async def scenario():
        state, _ = storage
        source, one, two = BrokenSource(), Target("one"), Target("two")
        await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 1 and not one.sent and not two.sent
        assert await state.get("cooldown:one") is None
        assert await state.get("cooldown:two") is None
        if status == 404:
            assert (await state.activity("123"))["status"] == "unsupported"
        else:
            assert await state.get("cooldown:garmin") > 0
        await Engine(state, None, source, [one, two], 1, 1).run("sync", DAY, DAY)
        assert source.downloads == 1

    asyncio.run(scenario())


def test_two_runs_cannot_own_same_journal(storage):
    async def scenario():
        state, _ = storage
        await state.initialize()
        await state.acquire()
        other = State(state.db)
        with pytest.raises(SyncError, match="another run"):
            await other.acquire()
        await state.release()
        await other.acquire()
        await other.release()

    asyncio.run(scenario())


def test_changed_original_is_not_overwritten(storage):
    async def scenario():
        state, archive = storage
        source = Source()
        await Engine(state, archive, source, []).run("sync", DAY, DAY)
        path = archive.root / "2026/10/123.fit"
        path.write_bytes(FIT[:-3] + b"y\x00\x00")
        with pytest.raises(SyncError, match="saved file changed"):
            await archive.read(await state.activity("123"))
        assert source.downloads == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["../x.fit", "/x.fit", "a\\b.fit", "C:x.fit", "nested.zip"])
def test_unsafe_zip_members_rejected(name):
    with pytest.raises(SyncError):
        extract_fit(zipped(name))


def test_zip_count_truncation_and_size_limits():
    packed = bytearray(zipped())
    end = packed.rfind(b"PK\x05\x06")
    struct.pack_into("<H", packed, end + 10, 50000)
    with pytest.raises(SyncError, match="directory"):
        extract_fit(packed)
    with pytest.raises(SyncError):
        extract_fit(zipped()[:-5])
    packed = bytearray(zipped())
    directory = packed.find(b"PK\x01\x02")
    struct.pack_into("<I", packed, directory + 24, 65 * 1024 * 1024)
    with pytest.raises(SyncError, match="64 MiB"):
        extract_fit(packed)


class HTTP(Transport):
    def __init__(self, responses):
        super().__init__()
        self.responses, self.calls = responses, []

    async def perform(self, method, address, headers, body, limit):
        self.calls.append((method, address, headers, body))
        return self.responses.pop(0)


def response(data, status=200):
    return Response(status, json.dumps(data).encode())


def test_garmin_plaintext_token_refresh_and_cache(storage):
    async def scenario():
        state, _ = storage
        await state.initialize()
        http = HTTP(
            [
                Response(200, b"head.payload.signature"),
                Response(401, b""),
                Response(200, b"new.payload.signature"),
                response([]),
                response([]),
            ]
        )
        garmin = Garmin("SECRET", http, state)
        await garmin.page(DAY, DAY, 0)
        await garmin.page(DAY, DAY, 0)
        assert len(http.calls) == 5
        assert http.calls[0][2] == {"Authorization": "ApiKey SECRET"}
        assert http.calls[-1][2] == {"Authorization": "Bearer new.payload.signature"}
        assert "limit=50" in http.calls[-1][1]

    asyncio.run(scenario())


def test_intervals_multipart_and_duplicate_response():
    async def scenario():
        http = HTTP([response({"id": "i1"}), response({"activities": [{"id": "i2"}]})])
        target = Intervals("secret", http, {})
        await target.connect()
        delivered = await target.send({"id": "123"}, FIT, "123.fit")
        assert delivered.state == "sent" and delivered.message == "already present"
        assert b'filename="123.fit"' in http.calls[1][3]
        assert FIT in http.calls[1][3] and "external_id=garmin-123" in http.calls[1][1]

    asyncio.run(scenario())


def test_es_preserves_revision_and_waits_for_publication():
    async def scenario():
        http = HTTP(
            [
                response(
                    {
                        "connection_id": "1",
                        "scopes": ["activities:write"],
                        "formats": ["fit", "zip"],
                    }
                ),
                response({"import_job_id": RECEIPT, "state": "pending"}, 202),
                response(
                    {
                        "import_job_id": RECEIPT,
                        "state": "succeeded",
                        "published": True,
                        "activity_id": "abc",
                    }
                ),
            ]
        )
        target = EffortSignal("secret", http, {})
        await target.connect()
        accepted = await target.send(
            {"id": "123", "revision": "2026-10-10T12:00:00Z"}, FIT, "123.fit"
        )
        delivered = await target.poll(accepted.receipt)
        assert accepted.state == "accepted" and delivered.state == "sent"
        assert "source_updated_at=2026-10-10T12%3A00%3A00Z" in http.calls[1][1]
        assert http.calls[1][3] == FIT

    asyncio.run(scenario())


def test_error_response_never_exposes_provider_body():
    with pytest.raises(SyncError) as caught:
        Response(401, b"key=TOP_SECRET and personal profile").check("provider")
    assert "TOP_SECRET" not in str(caught.value)


def test_history_cursor_resumes_and_explicit_send_respects_dates(storage):
    async def scenario():
        state, archive = storage
        source, target = Source(), Target()
        source.rows.append({"activityId": 124, "startTimeLocal": "2026-10-11", "ownerId": 456})
        await Engine(state, archive, source, [], 1, 1).run("import-history", DAY, "2026-10-11")
        await Engine(state, archive, source, [], 1, 1).run("import-history", DAY, "2026-10-11")
        assert source.downloads == 2
        await Engine(state, archive, None, [target]).run("send", "2026-10-11", "2026-10-11")
        assert [row[0] for row in target.sent] == ["124"]
        assert json.loads((await state.activity("123"))["targets"]) == []

    asyncio.run(scenario())
