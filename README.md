# Tradeguard

**Automated Stop-Loss Monitoring and Management for MyFundedPerps**

Tradeguard is a Python-based risk-management tool designed to help traders monitor open positions and manage protective stop-loss orders through the MyFundedPerps API.

It combines read-only position monitoring with optional automated stop management, including initial stop placement, break-even protection, and trailing-stop adjustments.

The goal is simple: make protective stop management more consistent, reduce manual oversight, and fail safely when account or order information is ambiguous.

> [!WARNING]
> **Trading risk:** Tradeguard can submit or modify live orders when its execution safeguards are deliberately enabled. This can affect funds, competition results, and account eligibility. Always begin in read-only mode, verify account permissions, and independently confirm that API automation is permitted by your trading provider. Tradeguard is provided as-is and does not guarantee successful order execution.

## Features

- **Position Monitoring:** Inspects open positions and working orders.
- **Initial Stop-Loss Planning:** Calculates proposed stop prices using entry price, configured risk percentage, and market tick size.
- **Break-Even Protection:** Supports moving eligible stop-loss orders toward entry after a configurable profit threshold.
- **Trailing Stop Management:** Supports tightening stops as a position moves favorably.
- **Protective Order Validation:** Checks position and order details before treating a stop as valid protection.
- **Execution Safeguards:** Uses fresh account snapshots, pre-write checks, and persistent attempt records to avoid blind retries.
- **Discord Notifications:** Optional alerts for important account and stop-management events.
- **Local State Management:** Stores management state and previous write attempts locally.

## Project Structure

| File                 | Description                                               |
| -------------------- | --------------------------------------------------------- |
| `main.py`            | Read-only account and stop-loss monitor                   |
| `auto_sl.py`         | Automated stop-loss watcher and manager                   |
| `one_shot_sl.py`     | Preview-first utility for a single stop-placement attempt |
| `get_position_id.py` | Retrieves account and position identifiers                |
| `launchd_runner.py`  | macOS background-process supervisor                       |
| `openapi.yaml`       | API specification reference                               |
| `tests/`             | Automated tests using mocked API interactions             |
| `deploy/`            | Example Linux systemd service configuration               |

## Technology

- Python
- MyFundedPerps REST API
- Requests
- Python-dotenv
- Unit testing with mocked API responses

## Installation

Clone the repository:

```
git clone https://github.com/gh0ulst/Tradeguard.git
cd Tradeguard
```

Create and activate a virtual environment:

```
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```
python -m pip install -r requirements.txt
```

Create your local environment file:

```
cp -n .env.example .env
```

Edit `.env` and add your own API credentials and account configuration.

**Never commit your `.env` file or expose API keys, account identifiers, Discord webhooks, or local trading-state files.**

## Configuration

Tradeguard uses environment variables for its configuration.

| Variable                | Description                             | Default        |
| ----------------------- | --------------------------------------- | -------------- |
| `MFP_BASE_URL`          | MyFundedPerps API endpoint              | Developers API |
| `MFP_API_KEY`           | API authentication key                  | Required       |
| `AUTO_SL_ACCOUNT_ID`    | Account monitored by the watcher        | Required       |
| `DISCORD_WEBHOOK_URL`   | Optional Discord notification webhook   | Disabled       |
| `SL_PERCENT`            | Initial stop distance percentage        | `1`            |
| `CHECK_INTERVAL`        | Watcher interval in seconds             | `30`           |
| `BREAKEVEN_R`           | Break-even activation threshold         | `1`            |
| `TRAILING_ACTIVATION_R` | Trailing-stop activation threshold      | `2`            |
| `TRAILING_DISTANCE_R`   | Trailing distance in R                  | `1`            |
| `DRY_RUN`               | Read-only safety setting                | `true`         |
| `AUTO_SL_EXECUTE`       | Additional live-execution authorization | `false`        |
| `AUTO_SL_UNATTENDED`    | Unattended execution authorization      | `false`        |

The watcher enforces a minimum polling interval of 15 seconds.

## Running Tradeguard

### 1. Read-only monitoring

Start with the read-only monitor:

```
python main.py
```

For continuous monitoring:

```
python main.py --watch
```

The monitor requires `DRY_RUN=true` and does not submit trading orders.

### 2. Automated stop management

Start the watcher without enabling live execution:

```
python auto_sl.py
```

Live execution requires the appropriate command-line and environment safeguards. Interactive execution also requires typed confirmation. Unattended execution has an additional authorization setting.

Do not enable live execution until you understand the implementation and have independently verified that your account permits automated order management.

### 3. One-time stop placement

The one-shot utility defaults to preview-only operation:

```
python one_shot_sl.py --position-id YOUR_POSITION_ID
```

Live placement requires its separate execution safeguards and confirmation.

## Stop Management Logic

Tradeguard supports configurable R-based stop management.

Example default configuration:

- Initial stop distance: 1%
- Break-even activation: 1R
- Trailing-stop activation: 2R
- Trailing distance: 1R

These are configuration defaults, not guaranteed risk limits or recommendations.

## Safety Architecture

Tradeguard includes safeguards designed to reduce unsafe automated order modifications:

- Exact account scoping.
- Position-side validation.
- Protective stop association checks.
- Position-size verification.
- Market tick-size-aware stop calculations.
- Fresh account snapshots before live modifications.
- Price checks before modifying stops.
- Persistent records of attempted writes.
- Manual-review requirements after uncertain API outcomes.
- Safety halts when important account or order information cannot be verified.

A working order is not automatically considered valid protection. Its relationship to the open position must be verified.

## Testing

Run the automated test suite:

```
python -m unittest discover -s tests -v
```

The tests use mocked API interactions and temporary state files.

**Important:** Passing automated tests does not establish that live exchange execution is safe or reliable. Live API behavior, order acceptance, and platform-specific rules require separate controlled validation.

## Runtime State and Security

Tradeguard creates local runtime files:

- `auto_sl_attempts.json`
- `auto_sl_state.json`

These files help prevent repeated attempts after uncertain order operations and maintain stop-management state.

Credentials, runtime state, logs, virtual environments, and local editor settings should remain private and are excluded from Git.

## Roadmap

- Read-only account and position monitoring
- Initial stop-loss calculations
- Break-even management
- Trailing-stop management
- Protective order validation
- Execution safeguards and persistent attempt tracking
- Automated tests for selected management logic
- Further validation against live API behavior in a controlled environment
- Expand API compatibility and error-handling coverage
- Improve configuration and user documentation
- Explore additional notification and monitoring integrations

## Disclaimer

Tradeguard is an independent community-built project and is not an official MyFundedPerps product.

It is a technical automation tool, not financial advice. Automated stop-loss systems cannot eliminate trading risk, slippage, outages, rejected orders, or unexpected market conditions. Traders remain responsible for monitoring their positions and understanding their provider's rules.
