"""Scheduled-only entry point: no HTTP endpoint can trigger uploads or reveal data."""

import logging
from datetime import date
from traceback import extract_tb

from js import AbortSignal, Object, fetch
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from gc2everywhere.cloudflare import CloudTransport, D1Database, R2Archive
from gc2everywhere.common import LOG, SyncError
from gc2everywhere.destinations import configured_destinations
from gc2everywhere.engine import Engine
from gc2everywhere.garmin import Garmin
from gc2everywhere.state import State


def convert(value):
    # Explicit conversion avoids leaving Python proxy objects attached to JS requests/bindings.
    return to_js(value, dict_converter=Object.fromEntries)


async def run(env):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = {
        name: str(getattr(env, name, "") or "")
        for name in (
            "GARMIN_API_KEY",
            "INTERVALS_API_KEY",
            "EFFORTSIGNAL_API_KEY",
            "EFFORTSIGNAL_URL",
            "COMMAND",
            "FROM",
            "TO",
            "DESTINATION",
            "ARCHIVE_ONLY",
        )
    }
    settings = {name: value for name, value in settings.items() if value}
    command = settings.get("COMMAND", "sync")
    if command not in ("sync", "import-history", "send", "status"):
        raise SyncError("COMMAND must be sync, import-history, send or status")
    start = date.fromisoformat(settings["FROM"]).isoformat() if "FROM" in settings else None
    end = date.fromisoformat(settings["TO"]).isoformat() if "TO" in settings else None
    if command == "import-history" and not start:
        raise SyncError("set FROM=YYYY-MM-DD for import-history")
    if start and end and start > end:
        raise SyncError("TO must be on or after FROM")
    state = State(D1Database(env.DB))
    if command == "status":
        await state.initialize()
        LOG.info("Status: %s", await state.summary())
        return
    transport = CloudTransport(fetch, convert, AbortSignal.timeout)
    # An omitted binding means forwarding only; existing deployments with ARCHIVE keep R2.
    binding = getattr(env, "ARCHIVE", None)
    archive = R2Archive(binding, convert) if binding is not None else None
    selected = settings.get("DESTINATION", "").split(",") if "DESTINATION" in settings else None
    destinations = (
        []
        if command == "import-history" or settings.get("ARCHIVE_ONLY") == "true"
        else (configured_destinations(settings, transport, selected))
    )
    if archive is None and (command != "sync" or not destinations):
        raise SyncError(
            "without R2, use sync with at least one destination key; archive modes need ARCHIVE"
        )
    LOG.info("Storage: %s", "R2 archive" if archive is not None else "forwarding only; D1 progress")
    garmin = None
    # After opting into R2, explicit send can retrieve older forwarding-only records if needed.
    if command in ("sync", "import-history") or (
        command == "send" and settings.get("GARMIN_API_KEY")
    ):
        if not settings.get("GARMIN_API_KEY"):
            raise SyncError("GARMIN_API_KEY secret is required")
        garmin = Garmin(settings["GARMIN_API_KEY"], transport, state)
    # One activity per run keeps both D1 queries and network/CPU work predictable for personal use.
    result = await Engine(state, archive, garmin, destinations, 1, 1).run(command, start, end)
    if result["errors"]:
        raise SyncError("run finished with errors; see per-activity logs")


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env=None, ctx=None):
        try:
            await run(self.env)
        except SyncError as error:
            LOG.error("%s", error)
            raise RuntimeError("Scheduled sync failed; see activity logs") from None
        except Exception as error:
            # Runtime exception messages can reflect binding contents; keep the Cron failure safe.
            locations = ", ".join(
                f"{frame.name}:{frame.lineno}" for frame in extract_tb(error.__traceback__)
            )
            LOG.error(
                "Scheduled sync failed: %s at %s; check configuration/bindings",
                type(error).__name__,
                locations,
            )
            raise RuntimeError("Scheduled sync failed") from None

    async def fetch(self, request):
        return Response("Not found", status=404)
