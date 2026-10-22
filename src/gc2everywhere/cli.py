"""Non-interactive commands: environment variables for keys, an argument for the archive."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .common import LOG, SyncError
from .destinations import DESTINATIONS, configured_destinations
from .engine import Engine
from .garmin import Garmin
from .local import LocalArchive, LocalDatabase, LocalTransport, state_path
from .state import State


def iso_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD, for example 2020-01-01") from error


def positive_integer(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parser():
    root = argparse.ArgumentParser(
        prog="gc2e",
        description="Save your Garmin recordings and send them to your services.",
        epilog="Keys: GARMIN_API_KEY, INTERVALS_API_KEY, EFFORTSIGNAL_API_KEY. No prompts.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    descriptions = {
        "sync": "Save new activities and send them to services with configured keys.",
        "import-history": "Save past activities; never sends them automatically.",
        "send": "Send archived activities that have not been delivered yet.",
        "status": "Show saved progress without making any network requests.",
    }
    for command, description in descriptions.items():
        sub = commands.add_parser(command, help=description, description=description)
        sub.add_argument(
            "--archive-dir", required=True, type=Path, help="folder for your recordings"
        )
        sub.add_argument("--state-dir", help="advanced: local journal directory, outside Dropbox")
        sub.add_argument("--json", action="store_true", help="print a machine-readable summary")
        sub.add_argument("--log-file", type=Path, help="also write a rotating log (3 × 2 MiB)")
        sub.add_argument("--verbose", action="store_true", help="include HTTP request counts")
        if command != "status":
            sub.add_argument(
                "--from",
                dest="start_day",
                type=iso_date,
                required=command == "import-history",
                help="first activity date, inclusive",
            )
            sub.add_argument(
                "--to", dest="end_day", type=iso_date, help="last activity date, inclusive"
            )
            sub.add_argument(
                "--max-activities",
                type=positive_integer,
                help="advanced: limit each download/delivery batch; resume next run",
            )
            sub.add_argument(
                "--destination",
                action="append",
                choices=[d.name for d in DESTINATIONS],
                help="send only to this service (can be repeated)",
            )
            sub.add_argument(
                "--archive-only", action="store_true", help="save files without sending"
            )
    return root


def configure_logging(verbose=False, log_file=None):
    LOG.handlers.clear()
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    if log_file:
        log_file = log_file.expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3)
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


async def execute(args, settings):
    if args.command != "status":
        if args.end_day and args.start_day and args.end_day < args.start_day:
            raise SyncError("--to must be on or after --from")
        if args.command == "send" and args.archive_only:
            raise SyncError("send cannot be combined with --archive-only")
        if args.command == "import-history" and args.destination:
            raise SyncError("import-history only archives files; use send afterwards")
        if (
            args.command in ("sync", "import-history")
            and not settings.get("GARMIN_API_KEY", "").strip()
        ):
            raise SyncError("GARMIN_API_KEY is required for downloading activities")
    archive = LocalArchive(args.archive_dir)
    database = LocalDatabase(state_path(archive, args.state_dir))
    state = State(database)
    try:
        await state.initialize()
        if args.command == "status":
            result = await state.summary()
        else:
            transport = LocalTransport()
            destinations = (
                []
                if args.archive_only or args.command == "import-history"
                else (configured_destinations(settings, transport, args.destination))
            )
            garmin = (
                Garmin(settings["GARMIN_API_KEY"].strip(), transport, state)
                if (args.command in ("sync", "import-history"))
                else None
            )
            LOG.info(
                "Starting %s archive=%s destinations=%s",
                args.command,
                archive.root,
                ",".join(d.name for d in destinations) or "archive only",
            )
            engine = Engine(state, archive, garmin, destinations, args.max_activities)
            result = await engine.run(args.command, args.start_day, args.end_day)
        if args.json:
            print(json.dumps(result, indent=2))
        elif args.command == "status":
            print("Archive:", archive.root)
            for item in result["activities"]:
                print(f"  {item['status']}: {item['count']}")
            for item in result["deliveries"]:
                print(f"  {item['destination']}: {item['count']} {item['state']}")
            print("Last run:", result["last_run"]["at"] if result["last_run"] else "not run yet")
            for item in result["errors"]:
                print(f"  activity={item['activity_id']} {item['destination']}: {item['error']}")
        return 1 if result.get("errors") and args.command != "status" else 0
    finally:
        database.close()


def main(argv=None):
    args = parser().parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    try:
        return asyncio.run(execute(args, os.environ))
    except SyncError as error:
        LOG.error("%s", error)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Unexpected OS/provider data must not dump an exception containing credentials.
        LOG.error("Could not complete the run; check archive permissions and saved metadata")
        return 1
    except KeyboardInterrupt:
        LOG.warning("Interrupted; completed files and delivery receipts are retained")
        return 130
