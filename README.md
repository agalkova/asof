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
jupyter lab notebooks/point_in_time_prototype.ipynb
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

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use mocked responses and do not require an API key or network access.

## Next

v0.2 will add full revision histories and check the point-in-time behavior across several economic series before any database or web application is introduced.
