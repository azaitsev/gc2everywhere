"""Small shared contracts keep the CLI and Worker on the same delivery path."""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

LOG = logging.getLogger("gc2e")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024


class SyncError(Exception):
    """An actionable, credential-free error suitable for the console."""


class BudgetReached(SyncError):
    """Stop a bounded run without losing its durable discovery/delivery progress."""


class HTTPError(SyncError):
    def __init__(self, service, status, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        hint = {
            401: "key expired or invalid",
            403: "access denied; check key permissions",
            409: "revision conflicts with an earlier upload",
            413: "file too large",
            429: "rate limit reached",
        }.get(status, "request was not accepted")
        super().__init__(f"{service}: HTTP {status} ({hint})")


def now_iso():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def activity_id(value):
    # Provider IDs become filenames and URLs; never accept arbitrary path components.
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,30}", value):
        raise SyncError("Garmin returned an invalid activity ID")
    return value


def url(base, path, **query):
    return base.rstrip("/") + path + ("?" + urlencode(query) if query else "")


def retry_seconds(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    def check(self, service):
        # Raw responses can contain personal data or reflected tokens. Never print them.
        if not 200 <= self.status < 300:
            raise HTTPError(service, self.status, retry_seconds(self.headers.get("retry-after")))
        return self

    def json(self, service):
        self.check(service)
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as error:
            raise SyncError(f"{service}: invalid JSON response") from error


@dataclass
class Delivery:
    state: str
    receipt: str = ""
    remote_ids: list = field(default_factory=list)
    message: str = ""


class Transport:
    def __init__(self, max_requests=None, max_file_bytes=MAX_FILE_BYTES):
        self.requests = 0
        self.max_requests = max_requests
        self.max_file_bytes = max_file_bytes

    async def request(self, method, address, *, headers=None, body=None, limit=MAX_JSON_BYTES):
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BudgetReached("request budget reached; remaining work is saved for the next run")
        self.requests += 1
        # Count every request, including authentication and receipt polls, against Worker limits.
        LOG.debug("HTTP %s request=%s", method, self.requests)
        return await self.perform(method, address, headers or {}, body, limit)

    async def perform(self, method, address, headers, body, limit):
        raise NotImplementedError
