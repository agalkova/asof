# AsOf

AsOf answers a simple question that revised datasets make surprisingly difficult:

> What did an economic series say on a particular date, using only the information available at the time?

FRED shows the latest version of a series. ALFRED preserves earlier releases. AsOf uses those historical vintages to prevent revised data from leaking into research, backtests, and retrospective analysis.

## Example

In September 2008, the reported value for August payroll employment was 137,473 thousand. The same observation now reads 137,211 thousand.

| Series | Observation | Known on 2008-09-12 | Current value | Revision |
| --- | --- | ---: | ---: | ---: |
| PAYEMS | 2008-08-01 | 137,473 | 137,211 | -262 |

The result was verified against ALFRED on October 22, 2026. The executed experiment is in [`notebooks/point_in_time_prototype.ipynb`](notebooks/point_in_time_prototype.ipynb).

## What it does

- Reconstructs the value known on a historical date.
- Retrieves the complete revision history for an observation.
- Serves point-in-time values with a bounded storage query instead of loading a full history.
- Syncs date ranges without silently truncating large ALFRED responses.
- Stores histories in SQLite or PostgreSQL.
- Serves stored data through a read-only FastAPI API.
- Refreshes a rolling window from the command line.

The core package is deliberately independent of any one deployment platform. SQLite keeps local work simple, PostgreSQL provides a conventional server database, and the planned AWS adapter will add DynamoDB without replacing either one.

## Project status

The local data engine, storage adapters, API, command-line tools, tests, and container are complete through v0.10.0. This release hardens point-in-time reads, validates public series identifiers, uses one package version across runtime and build metadata, and exercises PostgreSQL against a real service in CI.

AWS remains the planned portfolio deployment, not a running environment. The proposed design uses API Gateway, Lambda, DynamoDB, EventBridge Scheduler, CloudWatch, CDK, and GitHub Actions with OIDC. See [`docs/aws-architecture.md`](docs/aws-architecture.md) for the decisions, data model, publication protocol, security boundaries, and deployment plan.

## Setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[notebook]"
cp .env.example .env
```

Add your FRED API key to `.env`, then start JupyterLab:

```bash
jupyter lab
```

The local `.env` file is ignored by Git. API keys must not be placed in source code, notebooks, or command arguments.

### Optional dependencies

```bash
# PostgreSQL storage
pip install -e ".[postgres]"

# FastAPI server
pip install -e ".[api]"
```

Set `ASOF_DATABASE_URL` for PostgreSQL or `ASOF_DATABASE_PATH` to choose a local SQLite file. The generic `DATABASE_URL` variable is intentionally ignored.

## Command line

Load an explicit observation range:

```bash
asof sync \
  --series PAYEMS \
  --start 2008-07-01 \
  --end 2008-09-01
```

Refresh the latest 24 calendar months:

```bash
asof refresh --series PAYEMS --lookback-months 24
```

Create the configured database schema without fetching data:

```bash
asof init-db
```

Use `--sqlite path/to/asof.sqlite3` to override the configured database for one command. The FRED key is read only from `FRED_API_KEY`.

## API

Start the local API after loading data:

```bash
uvicorn asof.server:app --reload
```

The server opens SQLite in read-only mode. PostgreSQL startup also skips schema changes, allowing the API to use a read-only database role.

```text
GET /health
GET /series/{series_id}/observations
GET /series/{series_id}/observations/{observation_date}
GET /series/{series_id}/observations/{observation_date}/value?as_of=YYYY-MM-DD
```

Interactive documentation is available at `http://127.0.0.1:8000/docs` while the server is running.

## Python usage

```python
from asof import compare_vintages

comparison = compare_vintages(
    series_id="PAYEMS",
    observation_date="2008-08-01",
    historical_as_of="2008-09-12",
    api_key="...",
)

print(comparison.historical.value)
print(comparison.current.value)
print(comparison.revision)
```

Storage backends implement the same interface:

```python
from asof import SQLiteRevisionStore, sync_revision_histories

with SQLiteRevisionStore("data/asof.sqlite3") as store:
    result = sync_revision_histories(
        store=store,
        series_id="PAYEMS",
        observation_start="2008-07-01",
        observation_end="2008-09-01",
        api_key="...",
    )
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Unit tests use mocked FRED responses and require neither a network connection nor an API key. Local PostgreSQL integration tests run when `ASOF_TEST_DATABASE_URL` points to a disposable test database.

GitHub Actions starts PostgreSQL and runs the full suite, including the integration tests, on Python 3.11 and 3.13 for every push and pull request.

## Documentation

- [`docs/aws-architecture.md`](docs/aws-architecture.md) — planned v1.0 AWS design.
- [`docs/release-history.md`](docs/release-history.md) — development history from the first notebook onward.

## License

This project is available under the terms in [`LICENSE`](LICENSE).
