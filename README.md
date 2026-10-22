# gc2everywhere — Garmin Connect to Everywhere

Keep a copy of your Garmin workouts. Send them to the services you choose.

This Python tool uses Garmin's official **Personal API** to download original
activity files, save them in a folder you control, and send new recordings to
**Intervals.icu** and **Effort Signal**. A local Dropbox folder works like any
other folder. No Garmin password or browser automation is required.

## Why this exists

Independent services have built useful, sometimes unique features that Garmin
does not offer. They have helped people get more from their devices and given
them more reasons to buy Garmin products.

Garmin's move toward paid commercial API access makes life harder for those
services. Its current program distinguishes a limited Business Starter tier
from Enterprise access with negotiated commercial terms.

We think it is disappointing that Garmin has chosen a direction that puts more
of the burden on ordinary users, after independent developers helped make its
products more valuable. People should not have to become integration engineers
to keep using their own workout data. This small tool is our practical response:
keep your files, choose your services, and keep moving.

This is an independent project, not an official Garmin product.

**Disclaimer:** Provided "as is", without warranties, under the
[MIT License](https://github.com/azaitsev/gc2everywhere/blob/main/LICENSE).
To the extent permitted by applicable law, the authors and copyright holders
are not liable for losses or damages arising from its use, including data loss,
failed or duplicate transfers, account restrictions, or service charges. You are
responsible for backups, API keys, and complying with each service's terms.

## Install

You need **Python 3.11 or newer**. Install from PyPI in a virtual environment:

On macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install gc2everywhere
```

On Windows, in PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install gc2everywhere
```

The examples use `gc2e`. On Windows, use `.venv\Scripts\gc2e.exe` instead.
On macOS/Linux, activate `.venv` again in a new terminal or use the full path
to `.venv/bin/gc2e`.

To install from a source checkout instead, run `python -m pip install .` from
the repository directory with the virtual environment activated.

## Get your keys

**Garmin:** open [Personal API settings](https://connect.garmin.com/app/settings/personalApiToken)
in Garmin Connect, create a Personal API key, choose its expiry, and copy it
when shown. This is not an OAuth application client secret. Garmin offers
expiry choices of 30, 90, 180 days or one year. See the
[Personal API FAQ](https://developer.garmin.com/gc-developer-program/personal/personal-faq/).

**Intervals.icu:** sign in, open **Settings**, scroll to **Developer Settings**,
and create an **API key**. No athlete ID is needed: the tool uses the account
belonging to the key. See the maintainer's
[API access instructions](https://forum.intervals.icu/t/api-access-to-intervals-icu/609).

**Effort Signal:** open **Settings → Integrations → Personal API keys**, select
**Create API key**, give it a name such as `gc2everywhere`, allow
**activities:write**, and choose an expiry. Copy the key immediately, it is shown
once. Other data-write permissions are unnecessary for workout files. See the
[Personal API key guide](https://effortsignal.com/personal-api-key/) for creation,
permissions, usage, expiry, and revocation.

## First run

On macOS or Linux, paste your keys into these environment variables. Omit a
destination key if you do not want to send files to that service.

```sh
export GARMIN_API_KEY='your-garmin-personal-api-key'
export INTERVALS_API_KEY='your-intervals-api-key'
export EFFORTSIGNAL_API_KEY='your-effortsignal-personal-api-key'

gc2e sync --archive-dir "$HOME/Dropbox/Garmin"
```

On Windows PowerShell:

```powershell
$env:GARMIN_API_KEY = 'your-garmin-personal-api-key'
$env:INTERVALS_API_KEY = 'your-intervals-api-key'
$env:EFFORTSIGNAL_API_KEY = 'your-effortsignal-personal-api-key'
.venv\Scripts\gc2e.exe sync --archive-dir "$HOME\Dropbox\Garmin"
```

The folder is created if needed. There are no setup questions. The first sync
checks the last seven days, downloads recordings, and sends them to the services
whose keys you supplied. Later runs catch up from the last completed scan with a
seven-day overlap, so recent late uploads are found too. For older workouts,
use `import-history`.

For **archive only**, omit destination keys or add `--archive-only`.

Keys pasted into a terminal may remain in shell history. For regular use, keep
the exports in a private file outside Dropbox and this repository, restrict its
permissions (`chmod 600 /path/to/private-keys.sh`), and load it with
`source /path/to/private-keys.sh`. Never share keys in arguments, logs, screenshots
or scripts. Revoke and replace an exposed key.

## Four commands

| Command | What it does |
| --- | --- |
| `sync` | Downloads new recordings, sends them to configured services, and retries earlier deliveries. |
| `import-history` | Downloads past recordings. **Never sends them automatically.** |
| `send` | Explicitly sends saved, undelivered recordings. No Garmin key needed. |
| `status` | Shows local progress and errors. Makes no network requests. |

Save your history first:

```sh
gc2e import-history --from 2020-01-01 --archive-dir "$HOME/Dropbox/Garmin"
```

Send the saved history to one service when you are ready:

```sh
gc2e send --destination intervals --archive-dir "$HOME/Dropbox/Garmin"
```

Or select a date range:

```sh
gc2e send --destination effortsignal --from 2026-01-01 --to 2026-10-21 \
  --archive-dir "$HOME/Dropbox/Garmin"
```

Dates are inclusive and use the workout's local start date in Garmin. The
history available to your key is controlled by Garmin; a requested range cannot
override its access restrictions. `--destination` can be repeated.

Adding a destination key later **does not send the existing archive to it**.
Use `send` for that. Workouts discovered during `--archive-only` also need an
explicit `send`. A key for a different account creates a different delivery
identity; use it only if you intend to send files to that account.

## Files, progress, and retries

```text
Garmin/
  .gc2everywhere.json
  2026/10/
    123456789.fit
    123456789.zip
    123456789.json
```

The filename is the **Garmin activity ID**, not the workout name or a timestamp.
The ZIP is the unchanged original; FIT is extracted for easy use elsewhere.
JSON records the checksum and stable upload revision. A raw FIT response does
not create a ZIP.

Complete existing files are reused. Interrupted downloads never become finished
files. Repeated runs skip completed downloads and deliveries. A changed or
corrupt local file produces an error instead of silently replacing it. Manual
activities without a downloadable FIT are logged as skipped. ZIPs containing
several FIT files are not treated as one workout.

The delivery journal and Garmin's temporary access token live outside Dropbox,
under `~/.local/state/gc2everywhere/` by default (`XDG_STATE_HOME` is honored).
`--state-dir` overrides that location. Keep **one writer per archive**: a local
lease prevents overlapping runs using the same journal, but cannot coordinate
different computers writing to Dropbox.

After moving an archive or losing the journal, sidecars restore the inventory
without downloading files again. Previous service receipts cannot be recovered
locally, so restored history stays unsent until you run `send`. Receivers also
check duplicates; exactly-once delivery cannot be guaranteed if a connection
fails after the server accepts an upload.

Effort Signal's `accepted` means queued. Later runs poll its receipt and mark it
`sent` only after publication. `failed` and `needs_review` require inspection in
Effort Signal. Failure in one destination does not stop the others. Rate limits
honor `Retry-After`; retryable upload errors use increasing delays across runs.

This is a recording synchronizer, not a mirror of all Garmin data. It does not
sync wellness metrics, deletions, subsequent workout edits, or manually changed
files.

## Logs and scheduling

Logs include activity IDs, discovery, filenames, sizes, skips, each destination's
outcome, retries, and a final count. They exclude keys, authorization headers,
raw API responses, and FIT contents. IDs and dates are still personal data.

```sh
gc2e sync --archive-dir "$HOME/Dropbox/Garmin" --log-file "$HOME/gc2everywhere.log"
gc2e status --archive-dir "$HOME/Dropbox/Garmin"
```

Logs rotate at 2 MiB with three backups. `--verbose` adds HTTP request counts.
`--json` prints a machine-readable summary to stdout; logs use stderr.
Exit codes: `0` successful run, `1` operational error, `2` invalid arguments,
`130` interrupted.

For a large history, `--max-activities 50` bounds each download and destination
batch. Repeat the command to continue; completed work is retained.

Run `sync` every 30 minutes with your computer's scheduler. On macOS/Linux,
create a private script using absolute paths:

```sh
#!/bin/sh
. /absolute/path/to/private-keys.sh
exec /absolute/path/to/gc2everywhere/.venv/bin/gc2e sync \
  --archive-dir /absolute/path/to/Dropbox/Garmin \
  --log-file /absolute/path/to/gc2everywhere.log
```

Add `*/30 * * * * /bin/sh /absolute/path/to/sync-garmin.sh` with `crontab -e`.
On Windows, use Task Scheduler for a private PowerShell script with the
environment assignments and command above. A sleeping computer catches up
on its next run.

## Cloudflare Workers

**Experimental:** See `CLOUDFLARE_WORKERS.md` in the repository or the
[source distribution on PyPI](https://pypi.org/project/gc2everywhere/#files).
The Worker deployment files are included in that source archive; installing
the CLI with pip does not deploy a Worker.

## Contributing a destination

**Pull requests adding other services are welcome.** Destinations are independent
modules under `src/gc2everywhere/destinations/`. Implement the `Destination` contract:

1. Declare a name and the environment variable for its API key.
2. `connect()` verifies access and sets a stable account identity.
3. `send(row, content, filename)` uploads and returns a `Delivery`.
4. For asynchronous processing, return `accepted` with a receipt and implement
   `poll(receipt)`. Return `sent` only after confirmation.
5. Register the class in `DESTINATIONS`, document key creation, and add mocked
   tests for request format, failures, retries, and duplicates.

Use the shared transport so request limits and safe diagnostics work locally
and in Workers. Never log secrets or follow credential-bearing redirects.
Document Worker limitations if a service does not accept ZIP; local delivery
supplies FIT.

Developer checks, with no keys or live API calls:

```sh
uv sync --group dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```
