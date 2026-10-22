# AsOf

AsOf is a small Python project for querying economic data as it was known on a specific date. It uses FRED and ALFRED data to avoid treating today's revised history as if it had been available in real time.

## v0.1

The first prototype answers one concrete question:

> What did PAYEMS report for August 2008 as of September 12, 2008, and how does that compare with the value published today?

When verified on October 22, 2026, the result was:

| Series | Observation | As-of value | Current value | Revision |
| --- | --- | ---: | ---: | ---: |
| PAYEMS | 2008-08-01 | 137,473 thousand | 137,211 thousand | -262 thousand |

The code lives in `src/asof/fred.py`. The notebook in `notebooks/point_in_time_prototype.ipynb` contains the executed example.

## v0.2

The revision-history query returns each date when a single observation's published value changed. For August 2008 PAYEMS, ALFRED contains 16 published values between September 5, 2008 and February 7, 2025.

The same query was checked against five series:

| Series | Observation | Published values | Initial value | Latest value | Total revision |
| --- | --- | ---: | ---: | ---: | ---: |
| PAYEMS | 2008-08-01 | 16 | 137,473 | 137,211 | -262 |
| UNRATE | 2008-08-01 | 3 | 6.1 | 6.1 | 0.0 |
| CPIAUCSL | 2008-08-01 | 6 | 218.880 | 218.690 | -0.190 |
| GDPC1 | 2008-07-01 | 12 | 11,720.000 | 16,854.295 | 5,134.295 |
| FEDFUNDS | 2008-08-01 | 1 | 2.0 | 2.0 | 0.0 |

These values remain in each series' native units. The magnitudes are not directly comparable, and GDP history is affected by comprehensive revisions and changes to its reference year.

The executed table and PAYEMS revision chart are in `notebooks/revision_history.ipynb`.

## v0.3

Revision histories can now be stored in SQLite and loaded without calling FRED again. The database uses three tables:

```text
series
└── observations
    └── vintages
```

Each vintage is keyed by its series, observation date, and publication date. Saving a refreshed history replaces the stored snapshot for that observation in one transaction.

`notebooks/local_persistence.ipynb` fetches the PAYEMS example, writes all 16 vintages to `data/asof.sqlite3`, closes the connection, and verifies that the same history can be loaded after reopening the database. Local SQLite files under `data/` are ignored by Git.

## Setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[notebook]"
cp .env.example .env
```

Add your FRED API key to `.env`, then open the notebook:

```bash
jupyter lab
```

The local `.env` file is ignored by Git. Do not put an API key directly in code or notebook output.

## Python usage

```python
from asof import compare_vintages

result = compare_vintages(
    series_id="PAYEMS",
    observation_date="2008-08-01",
    historical_as_of="2008-09-12",
    api_key="...",
)

print(result.historical.value)
print(result.current.value)
print(result.revision)
```

To retrieve every changed value for one observation:

```python
from asof import get_revision_history

history = get_revision_history(
    series_id="PAYEMS",
    observation_date="2008-08-01",
    api_key="...",
)

for revision in history.revisions:
    print(revision.vintage_date, revision.value)
```

To keep the result locally:

```python
from asof import SQLiteRevisionStore

with SQLiteRevisionStore("data/asof.sqlite3") as store:
    store.save_history(history)
    restored = store.load_history(
        series_id="PAYEMS",
        observation_date="2008-08-01",
    )
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use mocked responses and do not require an API key or network access.

## Next

v0.4 will add bulk synchronization and a PostgreSQL implementation of the same schema. The web API comes after that.
