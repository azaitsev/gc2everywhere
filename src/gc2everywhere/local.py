"""Local disk and HTTP adapters; never imported by the Cloudflare entry point."""

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from urllib.error import HTTPError as URLHTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .common import LOG, MAX_FILE_BYTES, Response, SyncError, Transport, activity_id
from .files import extract_fit, validate_fit


class NoRedirect(HTTPRedirectHandler):
    # Keys must never follow a provider response to another origin.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalTransport(Transport):
    async def perform(self, method, address, headers, body, limit):
        return await asyncio.to_thread(self._request, method, address, headers, body, limit)

    def _request(self, method, address, headers, body, limit):
        request = Request(
            address,
            data=body,
            method=method,
            headers={
                "User-Agent": f"gc2everywhere/{__version__}",
                **headers,
            },
        )
        try:
            try:
                incoming = build_opener(NoRedirect()).open(request, timeout=45)
            except URLHTTPError as error:
                incoming = error
            with incoming:
                data = incoming.read(limit + 1)
                if len(data) > limit:
                    raise SyncError(f"response exceeds the {limit // (1024 * 1024)} MiB limit")
                return Response(
                    incoming.status,
                    data,
                    dict((key.lower(), value) for key, value in incoming.headers.items()),
                )
        except (OSError, ValueError) as error:
            raise SyncError("network request failed; check the connection and try again") from error


class LocalDatabase:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        os.chmod(path, 0o600)
        self.connection.row_factory = sqlite3.Row

    async def execute(self, sql, parameters=()):
        return [dict(row) for row in self.connection.execute(sql, parameters).fetchall()]

    def close(self):
        self.connection.close()


def atomic_write(path, content):
    # Publish complete files with rename: interrupted downloads never look like archived IDs.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".gc2e-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class LocalArchive:
    def __init__(self, directory):
        self.root = directory.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def base(self, row):
        day = date.fromisoformat(row["day"])
        return self.root / f"{day.year:04d}" / f"{day.month:02d}" / activity_id(row["id"])

    async def bind_owner(self, owner):
        path = self.root / ".gc2everywhere.json"
        if path.exists():
            if json.loads(path.read_text())["garmin_owner"] != owner:
                raise SyncError(
                    "this archive belongs to another Garmin account; choose another folder"
                )
        else:
            atomic_write(path, json.dumps({"garmin_owner": owner}).encode())

    async def lookup(self, row):
        base = self.base(row)
        fit_path = base.with_suffix(".fit")
        zip_path = base.with_suffix(".zip")
        metadata_path = base.with_suffix(".json")
        if not fit_path.exists() and not zip_path.exists():
            return None
        path = fit_path if fit_path.exists() else zip_path
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            raise SyncError(f"activity={row['id']}: existing archive file is unsafe or too large")
        content = path.read_bytes()
        fit = validate_fit(content) if path == fit_path else extract_fit(content)
        if not fit_path.exists():
            atomic_write(fit_path, fit)
        record = {
            "id": row["id"],
            "day": row["day"],
            "revision": row["revision"],
            "filename": str(fit_path.relative_to(self.root)),
            "size": len(fit),
            "sha256": hashlib.sha256(fit).hexdigest(),
        }
        if metadata_path.exists():
            saved = json.loads(metadata_path.read_text())
            if saved.get("sha256") != record["sha256"] or saved.get("id") != row["id"]:
                raise SyncError(
                    f"activity={row['id']}: saved file changed; restore the original file"
                )
            record["revision"] = saved["revision"]
        else:
            atomic_write(metadata_path, json.dumps(record, indent=2).encode())
        return record

    async def save(self, row, content):
        fit = extract_fit(content)
        base = self.base(row)
        if content.startswith(b"PK"):
            atomic_write(base.with_suffix(".zip"), content)
        atomic_write(base.with_suffix(".fit"), fit)
        return await self.lookup(row)

    async def read(self, row):
        record = await self.lookup(row)
        if not record:
            raise SyncError(f"activity={row['id']}: archived file is missing")
        return (self.root / record["filename"]).read_bytes(), f"{row['id']}.fit"

    async def restore(self, state):
        # Sidecars make a copied Dropbox archive usable even when its local delivery DB was lost.
        for path in sorted(self.root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.json")):
            if path.is_symlink() or path.stat().st_size > 16384:
                raise SyncError("archive metadata is unsafe or exceeds 16 KiB")
            try:
                row = json.loads(path.read_text())
                identifier = activity_id(row["id"])
                if path != self.base(row).with_suffix(".json"):
                    raise ValueError("path mismatch")
                if not await state.activity(identifier):
                    await state.discover(identifier, row["day"], row["revision"], [])
                    record = await self.lookup(row)
                    if record:
                        await state.archived(identifier, record)
                        LOG.info(
                            "activity=%s restored from archive; download not needed", identifier
                        )
            except (ValueError, KeyError, TypeError) as error:
                raise SyncError(f"invalid archive metadata: {path.name}") from error


def state_path(archive, override=None):
    if override:
        return Path(override).expanduser() / "state.sqlite3"
    # SQLite and short-lived tokens stay outside Dropbox to avoid multi-machine DB conflicts.
    identity = hashlib.sha256(str(archive.root).encode()).hexdigest()[:20]
    root = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return root / "gc2everywhere" / identity / "state.sqlite3"
