"""Independently deliver recordings, with optional durable archival before sending."""

import json
import time
from datetime import UTC, date, datetime, timedelta

from .common import LOG, BudgetReached, Delivery, HTTPError, SyncError, activity_id, now_iso
from .files import UnsupportedFile, original_record


class Engine:
    def __init__(self, state, archive, garmin, destinations, max_activities=None, page_size=50):
        self.state = state
        self.archive = archive
        self.garmin = garmin
        self.destinations = destinations
        self.max_activities = max_activities
        self.page_size = page_size
        self.counts = dict(discovered=0, downloaded=0, skipped=0, sent=0, accepted=0, errors=0)
        self.started = time.monotonic()
        # Retain at most one original during a run, reusing it across destinations without R2.
        self.original = None
        self.original_errors = {}
        self.download_attempts = 0

    async def discover(self, command, start_day, end_day):
        cursor_key = f"scan:{command}"
        cursor = await self.state.get(cursor_key)
        if cursor and (
            command == "sync"
            or (cursor.get("requested_from") == start_day and cursor.get("requested_to") == end_day)
        ):
            start_day, end_day, offset = cursor["from"], cursor["to"], cursor["offset"]
        else:
            offset = 0
            cursor = {
                "requested_from": start_day,
                "requested_to": end_day,
                "from": start_day,
                "to": end_day,
                "offset": 0,
            }
        pages = 0
        while True:
            await self.state.renew()
            LOG.info("Garmin: listing %s to %s offset=%s", start_day, end_day, offset)
            rows = await self.garmin.page(start_day, end_day, offset, self.page_size)
            for item in rows:
                if not isinstance(item, dict):
                    raise SyncError("Garmin activity list contains an invalid item")
                identifier = activity_id(item.get("activityId"))
                try:
                    day = date.fromisoformat(str(item["startTimeLocal"])[:10]).isoformat()
                except (KeyError, ValueError) as error:
                    raise SyncError(
                        f"activity={identifier}: Garmin start date is missing"
                    ) from error
                owner = str(item.get("ownerId", ""))
                if not owner or owner == "0":
                    raise SyncError("Garmin activity list is missing the account identity")
                if self.archive is not None:
                    await self.archive.bind_owner(owner)
                await self.state.bind_owner(owner)
                if not await self.state.activity(identifier):
                    targets = [d.name for d in self.destinations] if command == "sync" else []
                    await self.state.discover(identifier, day, now_iso(), targets)
                    self.counts["discovered"] += 1
                    LOG.info("activity=%s date=%s discovered", identifier, day)
                else:
                    LOG.info(
                        "activity=%s already known; no duplicate download scheduled", identifier
                    )
            offset += len(rows)
            cursor["offset"] = offset
            # Discovery commits IDs before advancing the page so a crash cannot lose an upload.
            await self.state.set(cursor_key, cursor)
            pages += 1
            if len(rows) < self.page_size:
                await self.state.set(cursor_key, None)
                if command == "sync":
                    await self.state.set("last_scan_day", end_day)
                return
            if self.max_activities is not None and pages * self.page_size >= self.max_activities:
                LOG.info("Garmin: page saved; listing will continue on the next run")
                return

    async def download(self, history_range=None):
        # Explicit history import may archive old forwarding metadata, without resetting receipts.
        condition = "status='pending'"
        if history_range:
            condition += " OR (status='ready' AND day>=? AND day<=?)"
        rows = await self.state.db.execute(
            f"SELECT * FROM activities WHERE {condition} ORDER BY (error != ''),day,id",
            history_range or (),
        )
        for row in rows[: self.max_activities]:
            await self.state.renew()
            identifier = row["id"]
            try:
                record = await self.archive.lookup(row)
                if record:
                    self.counts["skipped"] += 1
                    LOG.info(
                        "activity=%s found complete archive file; download skipped", identifier
                    )
                else:
                    LOG.info("activity=%s downloading original recording", identifier)
                    self.download_attempts += 1
                    content = await self.garmin.download(identifier)
                    await self.state.renew()
                    record = await self.archive.save(row, content)
                    self.counts["downloaded"] += 1
                    LOG.info(
                        "activity=%s saved %s bytes=%s",
                        identifier,
                        record["filename"],
                        record["size"],
                    )
                await self.state.archived(identifier, record)
            except UnsupportedFile as error:
                await self.state.db.execute(
                    "UPDATE activities SET status='unsupported', error=? WHERE id=?",
                    (str(error), identifier),
                )
                self.counts["skipped"] += 1
                LOG.warning("activity=%s skipped: %s", identifier, error)
            except HTTPError as error:
                if error.status == 404:
                    await self.state.db.execute(
                        "UPDATE activities SET status='unsupported', error=? WHERE id=?",
                        ("Garmin has no downloadable original", identifier),
                    )
                    LOG.warning("activity=%s no original file; summary retained", identifier)
                    self.counts["skipped"] += 1
                else:
                    raise
            except SyncError as error:
                if isinstance(error, BudgetReached):
                    raise
                self.counts["errors"] += 1
                await self.state.db.execute(
                    "UPDATE activities SET error=? WHERE id=?",
                    (str(error), identifier),
                )
                LOG.error("activity=%s download failed: %s", identifier, error)

    async def read_recording(self, row):
        identifier = row["id"]
        if self.original and self.original[0] == identifier:
            return self.original[1:]
        if identifier in self.original_errors:
            raise self.original_errors[identifier]
        if self.archive is not None:
            if row["status"] == "archived":
                return await self.archive.read(row)
            # Enabling R2 preserves receipts; only still-needed deliveries acquire an archive.
            record = await self.archive.lookup(row)
            if record:
                await self.state.archived(identifier, record)
                return await self.archive.read(row)
        if not self.garmin:
            raise SyncError("GARMIN_API_KEY is required to retrieve an unarchived recording")
        if await self.state.get("cooldown:garmin", 0) > time.time():
            raise SyncError("Garmin is waiting before retry; recording is not archived")
        if self.max_activities is not None and self.download_attempts >= self.max_activities:
            raise BudgetReached("download budget reached; remaining deliveries will resume")
        self.download_attempts += 1
        LOG.info("activity=%s downloading original for delivery", identifier)
        try:
            content = await self.garmin.download(identifier)
            self.counts["downloaded"] += 1
            await self.state.renew()
            record = original_record(row, content)
        except BudgetReached:
            raise
        except SyncError as error:
            if isinstance(error, HTTPError):
                if error.status == 404:
                    await self.state.db.execute(
                        "UPDATE activities SET status='unsupported', error=? WHERE id=?",
                        ("Garmin has no downloadable original", identifier),
                    )
                else:
                    await self.state.set(
                        "cooldown:garmin", time.time() + max(60, error.retry_after)
                    )
                # A source error must not incorrectly pause the destination's credentials/quota.
                error = SyncError(f"Garmin original unavailable: {error}")
            self.original_errors[identifier] = error
            raise error
        if self.archive is not None:
            record = await self.archive.save(row, content)
        await self.state.archived(identifier, record, persistent=self.archive is not None)
        # Preserve the discovery revision on re-download; Garmin may repack the same FIT in a ZIP.
        # Receivers deduplicate by source ID/revision and validate the actual recording contents.
        self.original = (identifier, content, record["filename"].rsplit("/", 1)[-1])
        LOG.info(
            "activity=%s %s bytes=%s",
            identifier,
            "archived" if self.archive is not None else "buffered for delivery; no archive",
            len(content),
        )
        return self.original[1:]

    async def send(self, start_day="0001-01-01", end_day="9999-12-31"):
        # Without an archive, fetch only after checking destination receipts and retry timers.
        statuses = (
            "'archived','ready'" if self.archive is not None else "'pending','ready','archived'"
        )
        rows = await self.state.db.execute(
            f"SELECT * FROM activities WHERE status IN ({statuses}) AND day>=? AND day<=? "
            "ORDER BY day,id",
            (start_day, end_day),
        )
        if not rows:
            return
        for destination in self.destinations:
            selected = [row for row in rows if destination.name in json.loads(row["targets"])]
            if not selected:
                continue
            # Respect a provider-wide Retry-After even when the next run selects another file.
            if await self.state.get(f"cooldown:{destination.name}", 0) > time.time():
                LOG.info("%s waiting before retry", destination.name)
                continue
            await self.state.renew()
            try:
                await destination.connect()
            except BudgetReached:
                raise
            except SyncError as error:
                self.counts["errors"] += 1
                if isinstance(error, HTTPError) and error.status == 429:
                    await self.state.set(
                        f"cooldown:{destination.name}", time.time() + max(60, error.retry_after)
                    )
                LOG.error(
                    "%s unavailable: %s; other destinations continue", destination.name, error
                )
                continue
            attempts_this_run = 0
            # One query per destination avoids exhausting D1's budget on old sent files.
            receipts = await self.state.db.execute(
                "SELECT * FROM deliveries WHERE destination=? AND account=?",
                (destination.name, destination.account),
            )
            receipts = {item["activity_id"]: item for item in receipts}
            for row in selected:
                previous = receipts.get(row["id"])
                if previous and previous["state"] in ("sent", "failed", "needs_review"):
                    LOG.info(
                        "activity=%s destination=%s state=%s; upload skipped",
                        row["id"],
                        destination.name,
                        previous["state"],
                    )
                    continue
                if previous and previous["next_attempt"] > time.time():
                    LOG.info(
                        "activity=%s destination=%s waiting before retry",
                        row["id"],
                        destination.name,
                    )
                    continue
                if self.max_activities is not None and attempts_this_run >= self.max_activities:
                    break
                attempts_this_run += 1
                await self.state.renew()
                attempts = (previous["attempts"] if previous else 0) + 1
                try:
                    if previous and previous["receipt"]:
                        LOG.info(
                            "activity=%s destination=%s checking publication",
                            row["id"],
                            destination.name,
                        )
                        delivery = await destination.poll(previous["receipt"])
                    else:
                        content, filename = await self.read_recording(row)
                        LOG.info(
                            "activity=%s destination=%s sending bytes=%s attempt=%s",
                            row["id"],
                            destination.name,
                            len(content),
                            attempts,
                        )
                        delivery = await destination.send(row, content, filename)
                    await self.state.renew()
                    await self.state.save_delivery(
                        row["id"], destination.name, destination.account, delivery, attempts
                    )
                    LOG.info(
                        "activity=%s destination=%s state=%s %s",
                        row["id"],
                        destination.name,
                        delivery.state,
                        delivery.message,
                    )
                    if delivery.state in ("sent", "accepted"):
                        self.counts[delivery.state] += 1
                    else:
                        self.counts["errors"] += 1
                except BudgetReached:
                    raise
                except SyncError as error:
                    delay = max(
                        min(30 * 2 ** min(attempts, 10), 86400), getattr(error, "retry_after", 0)
                    )
                    delivery = Delivery(
                        "retry", previous["receipt"] if previous else "", message=str(error)
                    )
                    await self.state.save_delivery(
                        row["id"],
                        destination.name,
                        destination.account,
                        delivery,
                        attempts,
                        time.time() + delay,
                    )
                    self.counts["errors"] += 1
                    LOG.error(
                        "activity=%s destination=%s failed: %s; retry in >=%ss",
                        row["id"],
                        destination.name,
                        error,
                        delay,
                    )
                    if isinstance(error, HTTPError) and error.status in (401, 403, 429):
                        await self.state.set(f"cooldown:{destination.name}", time.time() + delay)
                        break

    async def run(self, command, start_day=None, end_day=None):
        if self.archive is None and (command != "sync" or not self.destinations):
            raise SyncError("without R2, use sync with at least one destination key")
        await self.state.initialize()
        await self.state.acquire()
        complete = True
        try:
            if self.archive is not None:
                await self.archive.restore(self.state)
                # An opt-in archive must be bound even when this run's discovery page is empty.
                owner = await self.state.get("garmin_owner")
                if owner is not None:
                    await self.archive.bind_owner(owner)
            today = datetime.now(UTC).date()
            end_day = end_day or (today + timedelta(days=1)).isoformat()
            if command in ("sync", "import-history"):
                if not self.garmin:
                    raise SyncError("GARMIN_API_KEY is required for downloading activities")
                if not start_day:
                    previous = date.fromisoformat(
                        await self.state.get("last_scan_day", today.isoformat())
                    )
                    start_day = (min(previous, today) - timedelta(days=7)).isoformat()
                try:
                    if await self.state.get("cooldown:garmin", 0) > time.time():
                        LOG.info("Garmin: waiting before retry; archived deliveries can continue")
                    else:
                        await self.discover(command, start_day, end_day)
                        if self.archive is not None:
                            await self.download(
                                (start_day, end_day) if command == "import-history" else None
                            )
                except HTTPError as error:
                    # A provider outage must not prevent sending files already safely archived.
                    self.counts["errors"] += 1
                    await self.state.set(
                        "cooldown:garmin", time.time() + max(60, error.retry_after)
                    )
                    LOG.error("Garmin paused: %s", error)
            if command == "send":
                if not self.destinations:
                    raise SyncError("set INTERVALS_API_KEY or EFFORTSIGNAL_API_KEY to send files")
                rows = await self.state.db.execute(
                    "SELECT id, targets FROM activities WHERE day>=? AND day<=?",
                    (start_day or "0001-01-01", end_day),
                )
                changed = 0
                for row in rows:
                    # Explicit send authorizes only selected services, including future retries.
                    targets = sorted(
                        set(json.loads(row["targets"])) | {d.name for d in self.destinations}
                    )
                    if targets == sorted(json.loads(row["targets"])):
                        continue
                    if self.max_activities is not None and changed >= self.max_activities:
                        break
                    await self.state.db.execute(
                        "UPDATE activities SET targets=? WHERE id=?",
                        (json.dumps(targets), row["id"]),
                    )
                    changed += 1
            if command != "import-history":
                if command == "send":
                    await self.send(start_day or "0001-01-01", end_day)
                else:
                    await self.send()
        except BudgetReached as error:
            complete = False
            LOG.info("%s", error)
        except Exception:
            complete = False
            self.counts["errors"] += 1
            raise
        finally:
            # No recording bytes survive a forwarding run, including errors and cancellation.
            self.original = None
            result = {
                "at": now_iso(),
                "command": command,
                **self.counts,
                "batch_complete": complete,
                "seconds": round(time.monotonic() - self.started, 2),
            }
            await self.state.set("last_run", result)
            await self.state.release()
            LOG.info("Finished: %s", json.dumps(result, sort_keys=True))
        return result
