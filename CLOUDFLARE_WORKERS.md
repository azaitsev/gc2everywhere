# Cloudflare Workers

**Experimental:** Cloudflare Workers support is experimental. Processing can
vary with workout file size: a smaller recording may succeed while a larger one
reaches the Worker's memory, CPU, or execution limits. Test with your own
recordings before relying on this mode for regular syncing.

The included **Python Worker** forwards new recordings on a Cron Trigger.
By default it uses **D1** for progress/receipts and **Workers Secrets** for keys.
**R2 is optional**: no bucket or permanent file storage is needed for forwarding.
Workers cannot access your local Dropbox folder; use the CLI for that.

Without R2, each original stays in memory only during the run and is reused for
both destinations. A later failed-upload retry downloads it again from Garmin;
successful uploads and accepted uploads awaiting publication do not download or
send the file again. D1 retains IDs, metadata and receipts, not recording bytes.
Keep D1: without it, the Worker cannot remember completed deliveries. Retrying
an unarchived recording requires it to remain available in Garmin.

As with the CLI, the first sync checks the last seven days; later runs catch up
from the previous scan. Adding another destination does not resend old workouts.
Configure at least one destination key when running without R2.

The Worker forwards original ZIPs without decompressing them. Intervals accepts
ZIP. Effort Signal must advertise `zip` in its ingest connection formats; a server
without that update leaves the upload queued for retry. Local CLI uploads use
FIT and do not depend on the ES ZIP update.

## Set up

Install [Node.js](https://nodejs.org/) and [uv](https://docs.astral.sh/uv/), then
run these commands from the repository. They create resources in **your**
Cloudflare account; review your plan and D1 charges first.

```sh
uv sync --group workers
npm ci
cp wrangler.toml wrangler.local.toml
uv run --group workers pywrangler login
uv run --group workers pywrangler d1 create gc2everywhere-state
```

Replace the placeholder `database_id` in `wrangler.local.toml` with the ID from
the last command. Update the database name too if you changed it. Leave
`workers_dev` and `preview_urls` disabled. The first run
creates the database schema automatically.

Set the Garmin secret and whichever destination secrets you need, then deploy:

```sh
uv run --group workers pywrangler secret put GARMIN_API_KEY --config wrangler.local.toml
uv run --group workers pywrangler secret put INTERVALS_API_KEY --config wrangler.local.toml
uv run --group workers pywrangler secret put EFFORTSIGNAL_API_KEY --config wrangler.local.toml
uv run --group workers pywrangler deploy --config wrangler.local.toml
```

Cloudflare's secret commands ask you to paste each key. Never put keys in the
configuration file. The Cron schedule is every 30 minutes, in UTC. View runs in
the Cloudflare dashboard or with:

```sh
uv run --group workers pywrangler tail --config wrangler.local.toml
```

## Optional R2 archive

To retain original files, create a **private** R2 bucket:

```sh
uv run --group workers pywrangler r2 bucket create gc2everywhere-archive
```

Uncomment these lines in `wrangler.local.toml`, update the name if necessary,
and deploy again:

```toml
[[r2_buckets]]
binding = "ARCHIVE"
bucket_name = "gc2everywhere-archive"
```

The `ARCHIVE` binding enables archival automatically. Existing deployments with
that binding retain their behavior. Remove the binding and deploy to return to
forwarding only; this does not delete the bucket or its files. Keep the same D1
database to preserve delivery receipts when switching modes. Enabling R2 saves
new recordings and any recordings still needed for delivery; it does not copy
previously forwarded history into R2 automatically.
Use `import-history` for that. Sending a previously unarchived recording after
enabling R2 also requires the Garmin key so the Worker can retrieve it.

## History and status

`COMMAND = "status"` works in both modes and writes progress in the next Cron
log without contacting Garmin or destinations. In its activity counts, `ready`
means metadata was recorded without a permanent archive; `archived` means a file
was saved. Delivery counts show which services accepted or published it.

**R2 is required** for `import-history`, `send`, and `ARCHIVE_ONLY = "true"`.
Without R2, use `sync` to forward new recordings and retry unfinished deliveries.
For a local history archive, use the CLI instead.

For history, set these values under `[vars]` and deploy again:

```toml
COMMAND = "import-history"
FROM = "2020-01-01"
TO = "2026-10-21"
```

It saves one original per run and resumes its page cursor. Once history is
archived, restore `COMMAND = "sync"` and remove `FROM` and `TO`.
To send history, use `COMMAND = "send"` with the dates and optionally
`DESTINATION = "intervals"` or `"effortsignal"`. Restore `sync` afterwards.
`ARCHIVE_ONLY = "true"` disables sending during sync.

R2 uses year/month/activity ID filenames. Object metadata contains the checksum
and stable revision. If D1 is lost, re-run `import-history` for the relevant
dates: existing R2 originals are reused without downloading again. Preserve D1
too if you need receipts. Use dedicated resources for each Garmin account.

## Limits

Each Worker run handles **at most one download and one delivery per destination**.
The original-file limit is **16 MiB**; buffers are bounded and ZIPs are never
expanded. Backlogs take several runs, and large histories can take days. Use the
local CLI for faster imports or larger files (local limit: 64 MiB).

Cloudflare Free has a very small per-invocation CPU allowance. Running less
often does not increase it. This project **does not promise** that a full Python
sync fits Free: measure CPU usage with your files before relying on it. D1 and
optional R2 have separate quotas and possible charges. See
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[R2 pricing](https://developers.cloudflare.com/r2/pricing/), and
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).

Public HTTP routes are disabled and the HTTP handler only returns 404. There
is no public trigger or file-download endpoint. Keep D1 private too: it contains
a temporary Garmin token.

## Local build check

For a local Worker bundle check, install the `workers` group and Node packages,
then run `uv run --group workers pywrangler deploy --dry-run`. A local build does
not prove a deployed Cron run or its CPU usage.

[Back to README](README.md).
