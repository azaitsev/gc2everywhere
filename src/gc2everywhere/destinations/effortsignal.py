"""An ES upload is delivered only after its import receipt confirms publication."""

import re
from urllib.parse import urlsplit

from ..common import Delivery, SyncError, url
from .base import Destination


class EffortSignal(Destination):
    name = "effortsignal"
    key_variable = "EFFORTSIGNAL_API_KEY"

    def __init__(self, key, transport, settings):
        super().__init__(key, transport, settings)
        self.base_url = settings.get("EFFORTSIGNAL_URL", "https://effortsignal.com").rstrip("/")
        parsed = urlsplit(self.base_url)
        # A custom instance is supported, but never put a key in a URL or send it over plaintext.
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise SyncError("EFFORTSIGNAL_URL must be an HTTPS origin without path or credentials")
        self.formats = []

    def headers(self):
        return {"Authorization": f"Bearer {self.key}"}

    async def connect(self):
        response = await self.http.request(
            "GET",
            url(self.base_url, "/api/v1/ingest/connection/"),
            headers=self.headers(),
        )
        data = response.json("Effort Signal")
        if not isinstance(data, dict) or not data.get("connection_id"):
            raise SyncError("Effort Signal: connection identity is missing")
        if "activities:write" not in data.get("scopes", []):
            raise SyncError("Effort Signal key needs the activities:write permission")
        self.account = self.base_url + ":" + str(data["connection_id"])
        self.formats = data.get("formats", [])

    def result(self, data):
        if not isinstance(data, dict):
            raise SyncError("Effort Signal: invalid import receipt")
        receipt = str(data.get("import_job_id", ""))
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", receipt):
            raise SyncError("Effort Signal: import_job_id is missing or invalid")
        state = data.get("state")
        if state == "succeeded" and data.get("published") is True:
            return Delivery("sent", receipt, [str(data.get("activity_id", ""))], "published")
        if state in ("failed", "needs_review"):
            return Delivery(state, receipt, message="check this import in Effort Signal")
        if state not in ("pending", "running", "succeeded"):
            raise SyncError("Effort Signal: unknown import state")
        return Delivery("accepted", receipt, message="accepted; waiting for publication")

    async def send(self, row, content, filename):
        if filename.rsplit(".", 1)[-1] not in self.formats:
            raise SyncError("Effort Signal does not support this format yet; update the ES server")
        response = await self.http.request(
            "POST",
            url(
                self.base_url,
                "/api/v1/ingest/activities/",
                external_id=f"garmin-{row['id']}",
                source_updated_at=row["revision"],
                filename=filename,
            ),
            headers={**self.headers(), "Content-Type": "application/octet-stream"},
            body=content,
        )
        return self.result(response.json("Effort Signal upload"))

    async def poll(self, receipt):
        # Poll saved receipts, never re-upload an already accepted file just because it is pending.
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", receipt):
            raise SyncError("invalid saved Effort Signal receipt")
        response = await self.http.request(
            "GET",
            url(self.base_url, f"/api/v1/ingest/imports/{receipt}/"),
            headers=self.headers(),
        )
        return self.result(response.json("Effort Signal import status"))
