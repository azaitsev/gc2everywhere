"""Use Garmin's official Personal API; no Garmin passwords or scraping sessions."""

import hashlib
import json
import re
import time

from .common import LOG, HTTPError, SyncError, activity_id, url

BASE_URL = "https://personal.connectapi.garmin.com"


class Garmin:
    def __init__(self, key, transport, state):
        self.key = key
        self.http = transport
        self.state = state

    async def token(self, refresh=False):
        fingerprint = hashlib.sha256(self.key.encode()).hexdigest()
        saved = await self.state.get("garmin_token", {})
        if (
            not refresh
            and saved.get("key") == fingerprint
            and saved.get("expires", 0) > time.time()
        ):
            return saved["token"]
        LOG.info("Garmin: obtaining an access token")
        response = await self.http.request(
            "POST",
            url(BASE_URL, "/personalapitoken-service/personal-api/personal-api-key/token"),
            headers={"Authorization": f"ApiKey {self.key}"},
        )
        response.check("Garmin authentication")
        # The Personal API can return the JWT as plain text instead of an OAuth JSON object.
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            data = response.body.decode("ascii", errors="replace").strip()
        token = data.get("access_token") if isinstance(data, dict) else data
        if not isinstance(token, str) or not token:
            raise SyncError("Garmin authentication: access_token is missing")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise SyncError("Garmin authentication: expected an access token")
        # Garmin uses a 24-hour token without refresh tokens. Leave a margin for clock skew.
        expires = (
            min(float(data.get("expires_in", 86400)), 86400) if isinstance(data, dict) else 86400
        )
        await self.state.set(
            "garmin_token",
            {
                "key": fingerprint,
                "token": token,
                "expires": time.time() + max(0, expires - 300),
            },
        )
        return token

    async def request(self, path, *, max_bytes, **query):
        for refresh in (False, True):
            token = await self.token(refresh)
            response = await self.http.request(
                "GET",
                url(BASE_URL, path, **query),
                headers={"Authorization": f"Bearer {token}"},
                limit=max_bytes,
            )
            if response.status != 401 or refresh:
                return response.check("Garmin")
        raise HTTPError("Garmin", 401)

    async def page(self, start_date, end_date, offset, limit=50):
        response = await self.request(
            "/activitylist-service/activities/search/activities",
            max_bytes=2 * 1024 * 1024,
            startDate=start_date,
            endDate=end_date,
            start=offset,
            limit=limit,
            sortBy="startLocal",
            sortOrder="asc",
            excludeChildren="true",
        )
        data = response.json("Garmin activity list")
        if not isinstance(data, list):
            raise SyncError("Garmin activity list: expected an array")
        return data

    async def download(self, identifier):
        response = await self.request(
            f"/download-service/files/activity/{activity_id(identifier)}",
            max_bytes=self.http.max_file_bytes,
        )
        return response.body
