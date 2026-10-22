"""Intervals accepts FIT/ZIP and deduplicates uploads using the original file hash."""

import base64
import hashlib

from ..common import Delivery, SyncError, url
from .base import Destination


class Intervals(Destination):
    name = "intervals"
    key_variable = "INTERVALS_API_KEY"
    base_url = "https://intervals.icu"

    def headers(self):
        credentials = base64.b64encode(f"API_KEY:{self.key}".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}

    async def connect(self):
        response = await self.http.request(
            "GET",
            url(self.base_url, "/api/v1/athlete/0"),
            headers=self.headers(),
        )
        data = response.json("Intervals.icu")
        if not isinstance(data, dict) or not data.get("id"):
            raise SyncError("Intervals.icu: athlete identity is missing")
        self.account = str(data["id"])

    async def send(self, row, content, filename):
        # A deterministic envelope makes retries easy to inspect; dedup uses the enclosed bytes.
        boundary = "gc2e" + hashlib.sha256(content).hexdigest()[:32]
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        response = await self.http.request(
            "POST",
            url(self.base_url, "/api/v1/athlete/0/activities", external_id=f"garmin-{row['id']}"),
            headers={**self.headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=body,
        )
        data = response.json("Intervals.icu upload")
        if not isinstance(data, dict):
            raise SyncError("Intervals.icu upload: expected activity identifiers")
        rows = data.get("activities") or ([data] if data.get("id") else [])
        identifiers = [
            str(item["id"]) for item in rows if isinstance(item, dict) and item.get("id")
        ]
        if not identifiers:
            raise SyncError("Intervals.icu upload: no activity identifiers returned")
        return Delivery(
            "sent",
            remote_ids=identifiers,
            message="already present" if response.status == 200 else "created",
        )
