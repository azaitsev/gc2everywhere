"""Cloudflare bindings keep progress in D1 and optionally archive originals in R2."""

import hashlib
import json
import time
from datetime import date

from .common import BudgetReached, Response, SyncError, Transport, activity_id
from .files import original_record


async def read_stream(stream, limit):
    # Stop on actual bytes, even when a server omits or lies about Content-Length.
    if not stream:
        return b""
    reader = stream.getReader()
    content = bytearray()
    try:
        while True:
            chunk = await reader.read()
            if chunk.done:
                return bytes(content)
            # SDK-wrapped R2 streams already return Python buffers; raw Fetch streams do not.
            value = chunk.value
            if not isinstance(value, (bytes, bytearray, memoryview)):
                value = value.to_py()
            if len(content) + len(value) > limit:
                await reader.cancel()
                raise SyncError("file exceeds this Worker's memory-safe size limit")
            content.extend(value)
    finally:
        reader.releaseLock()


class CloudTransport(Transport):
    def __init__(self, fetch, convert, timeout):
        # Leave room for R2/D1 operations and bound buffers within Workers' 128 MiB memory.
        super().__init__(max_requests=20, max_file_bytes=16 * 1024 * 1024)
        self.fetch = fetch
        self.convert = convert
        self.timeout = timeout
        self.started = time.monotonic()

    async def perform(self, method, address, headers, body, limit):
        if time.monotonic() - self.started > 120:
            raise BudgetReached("Worker time budget reached; remaining work will resume")
        options = {
            "method": method,
            "headers": headers,
            "redirect": "manual",
            "signal": self.timeout(45000),
        }
        if body is not None:
            options["body"] = body
        try:
            response = await self.fetch(address, self.convert(options))
            content = await read_stream(response.body, limit)
            return Response(
                response.status,
                content,
                {
                    "retry-after": response.headers.get("retry-after"),
                },
            )
        except SyncError:
            raise
        except Exception:
            # JS errors may include request details; expose only a safe diagnostic.
            raise SyncError("Worker network request failed; retry on the next run") from None


class D1Database:
    def __init__(self, binding):
        self.binding = binding

    async def execute(self, sql, parameters=()):
        statement = self.binding.prepare(sql)
        if parameters:
            statement = statement.bind(*parameters)
        result = await statement.all()
        # The current SDK converts binding results to dicts; raw JS proxies remain supported.
        return (result if isinstance(result, dict) else result.to_py())["results"]


class R2Archive:
    def __init__(self, binding, convert, max_bytes=16 * 1024 * 1024):
        self.binding = binding
        self.convert = convert
        self.max_bytes = max_bytes
        self.owner = None

    def base(self, row):
        day = date.fromisoformat(row["day"])
        return f"{day.year:04d}/{day.month:02d}/{activity_id(row['id'])}"

    async def bind_owner(self, owner):
        if self.owner == owner:
            return
        key = ".gc2everywhere.json"
        existing = await self.binding.get(key)
        if existing:
            metadata = json.loads(await read_stream(existing.body, 16384))
            if metadata.get("garmin_owner") != owner:
                raise SyncError("R2 archive belongs to another Garmin account")
        else:
            await self.binding.put(key, json.dumps({"garmin_owner": owner}))
        self.owner = owner

    async def lookup(self, row):
        for extension in ("zip", "fit"):
            filename = self.base(row) + "." + extension
            item = await self.binding.head(filename)
            if item:
                # The object and its metadata are committed by the same atomic R2 put.
                metadata = item.customMetadata
                metadata = metadata if isinstance(metadata, dict) else metadata.to_py()
                record = json.loads(metadata["gc2e"])
                if (
                    record.get("id") != row["id"]
                    or record.get("filename") != filename
                    or record.get("size") != item.size
                    or item.size > self.max_bytes
                ):
                    raise SyncError("R2 original or metadata changed; restore the original object")
                return record
        return None

    async def save(self, row, content):
        # Keep compressed originals in the Worker: receivers validate/decode the FIT envelope.
        # In particular, do not decompress untrusted ZIPs within the Worker CPU/memory budget.
        record = original_record(row, content, self.max_bytes)
        await self.binding.put(
            record["filename"],
            self.convert(content),
            self.convert(
                {
                    "customMetadata": {"gc2e": json.dumps(record)},
                    "httpMetadata": {"contentType": "application/octet-stream"},
                }
            ),
        )
        return record

    async def read(self, row):
        record = await self.lookup(row)
        if not record:
            raise SyncError("R2 original is missing")
        item = await self.binding.get(record["filename"])
        if not item:
            raise SyncError("R2 original disappeared before sending")
        content = await read_stream(item.body, self.max_bytes)
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise SyncError("R2 original changed; restore the original object")
        return content, record["filename"].rsplit("/", 1)[-1]

    async def restore(self, state):
        # Avoid listing an entire R2 history each Cron run. Re-importing the same Garmin dates
        # rebuilds a lost D1 inventory and lookup() reuses each existing original by activity ID.
        return None
