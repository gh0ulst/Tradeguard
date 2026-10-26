# MyFundedPerps Auto-SL

Python tools for monitoring MyFundedPerps accounts and managing protective
stop-loss orders. The project includes a read-only monitor, an optional
automatic stop manager, a one-attempt stop test, and unit tests.

> **Trading risk:** `auto_sl.py` can submit or update live orders when its
> explicit execution safeguards are enabled. This can affect funds, competition
> results, or account eligibility. Start in read-only mode, review every
> configuration value, and confirm that API automation is permitted for your
> account. This software is provided without a guarantee that an exchange API,
> quote, or protective order will be available or correct.

## Features

- `main.py` scans accounts, positions, and working orders using read-only API
  requests. It reports stop status and can print a dry-run request preview; it
  does not submit trading requests.
- `auto_sl.py` scopes the watcher to one exact account, checks protective-stop
  coverage, and supports initial stops, break-even management, and trailing-stop
  updates. It rechecks state before writes, avoids blind retries, and records
  attempted writes and management state in local JSON files.
- Optional Discord alerts report issues without including the webhook URL.
- `one_shot_sl.py` supports a single, separately confirmed stop placement
  attempt for a specified position.
- `get_position_id.py` reads account and position identifiers for local setup.
- `launchd_runner.py` suppresses automatic restarts after a safety halt.
  `deploy/auto-sl.service` is a systemd unit example; review its live-execution
  settings before using it.

## Requirements

- Python 3.9 or newer
- MyFundedPerps API access
- Dependencies from [`requirements.txt`](./requirements.txt)

## Setup

Run these commands from the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Edit `.env` locally. Replace the API-key and account-ID placeholders with your
own values, and keep the file private. Do not commit `.env`, share account or
position identifiers, or paste credentials into logs or support requests.
`DISCORD_WEBHOOK_URL` is optional; leave it empty to disable Discord alerts.

## Configuration

| Variable | Purpose | Default/example |
| --- | --- | --- |
| `MFP_BASE_URL` | API endpoint; this version of the automatic watcher requires the live endpoint | `https://developers.myfundedperpetuals.com` |
| `MFP_API_KEY` | API bearer credential | Required; keep private |
| `AUTO_SL_ACCOUNT_ID` | Exact account scope for `auto_sl.py` | Required; keep private |
| `DISCORD_WEBHOOK_URL` | Optional Discord alert webhook | Empty (disabled) |
| `SL_PERCENT` | Initial stop distance as a percentage | `1` |
| `CHECK_INTERVAL` | Watch interval in seconds; the code enforces a 15-second minimum | `30` |
| `BREAKEVEN_R` | Profit threshold for break-even management, in R | `1` |
| `TRAILING_ACTIVATION_R` | Profit threshold to activate trailing, in R | `2` |
| `TRAILING_DISTANCE_R` | Trailing distance, in R | `1` |
| `DRY_RUN` | Required as `true` by the read-only monitor and one-shot tool | `true` |
| `AUTO_SL_EXECUTE` | Additional live-write lock for `auto_sl.py` | `false` |
| `AUTO_SL_UNATTENDED` | Additional unattended-start lock when used with the service runner | `false` |

## Running

The monitor is read-only and requires `DRY_RUN=true`:

```sh
python main.py
python main.py --watch
```

The automatic watcher is also read-only unless both its command-line and
environment safeguards permit writes. Without `--execute`, it prints a
read-only status:

```sh
python auto_sl.py
```

Do not enable live writes or unattended operation unless you understand the
consequences and have independently confirmed your account's rules. In
interactive execution mode the program requires an additional typed
confirmation. Unattended execution requires separate explicit environment
authorization. Never deploy the systemd example without reviewing its
`ExecStart` and the corresponding environment file permissions.

The one-shot utility defaults to preview-only behavior and requires a position
ID. The script has a separate execution flag, an additional environment
safeguard, and a typed confirmation before its single live write:

```sh
python one_shot_sl.py --position-id YOUR_LOCAL_POSITION_ID
```

`get_position_id.py` queries the API and prints account and position IDs. Run
it only in a private terminal and do not share its output.

## Tests

Run the automated test suite without making API calls:

```sh
python -m unittest discover -s tests -v
```

The tests use mocked API interactions and temporary state files. They do not
test live exchange behavior.

## Local state and deployment

The watcher writes `auto_sl_attempts.json` and `auto_sl_state.json` beside its
source file. These files can contain trading-related runtime state and are
excluded from Git. Back up and protect them locally; do not upload them. The
same exclusion applies to local logs, credentials, virtual environments, and
editor settings.

`deploy/auto-sl.service` is a Linux systemd example, not a LaunchAgent
configuration. Do not replace or restart any existing production service as
part of installing or testing this repository.
