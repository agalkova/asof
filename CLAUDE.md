# AsOf

Point-in-time queries for revised economic data. Given a FRED series, an observation date and an as-of date, AsOf returns the value that was published on that date, using ALFRED vintages, so revised data never leaks into backtests.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api,postgres,dynamodb,dev]"   # everything the test suite exercises

python -m unittest discover -s tests -v          # full suite, about 40 s
python -m unittest tests.test_store_contract -v  # one module
python -m unittest tests.test_dynamodb.PublicationTests.test_first_publication_stores_manifest_generation_and_cleanup_record  # one test
```

- Tests use `unittest`, not pytest. Do not add pytest or a new test runner.
- FRED calls are mocked. Tests need no network and no API key.
- DynamoDB tests run against `moto` in process and are skipped when `.[dynamodb,dev]` is not installed. A skip is not a pass: install the extras before you report results.
- PostgreSQL integration tests run only when `ASOF_TEST_DATABASE_URL` points at a disposable database. CI (`.github/workflows/tests.yml`) runs them on Python 3.11 and 3.13.
- There is no linter or type checker configured. Match the surrounding style: type hints on public functions, `from __future__ import annotations`, keyword-only arguments for lookups.

## Layout

| Path | What it owns |
| --- | --- |
| `src/asof/models.py` | Frozen dataclasses: `RevisionHistory`, `VintageValue`, `AsOfLookup` |
| `src/asof/fred.py` | ALFRED client, chunked range requests, `VintageLimitError` |
| `src/asof/storage.py` | `RevisionStore` protocol and the SQLite backend |
| `src/asof/postgres.py` | PostgreSQL backend |
| `src/asof/dynamodb.py` | DynamoDB backend with generation-pointer publication |
| `src/asof/dynamodb_cleanup.py` | Removal of retired and abandoned DynamoDB generations |
| `src/asof/sync.py` | Fetch-and-store orchestration |
| `src/asof/api.py`, `server.py`, `run_server.py` | Read-only FastAPI service |
| `src/asof/cli.py` | `asof` command line |
| `tests/test_store_contract.py` | Behavior every backend must share |
| `docs/aws-architecture.md` | Source of truth for the AWS design and its delivery sequence |

## Invariants

- **No look-ahead.** A lookup as of date D must never return a vintage published after D. Any change to a read path needs a test that proves this.
- **One contract, many backends.** Behavior shared by all stores belongs in `RevisionStore` and is tested in `tests/test_store_contract.py` against every backend. Do not add backend-specific behavior callers have to know about.
- **Atomic visibility.** Each observation history becomes visible all at once (`save_histories` docstring). In DynamoDB that is the generation pointer flip described in `docs/aws-architecture.md`. Readers must never see a half-written generation.
- **Cleanup is safe and resumable.** Cleanup never deletes the live generation and can be interrupted and rerun at any point.
- **The core stays platform-independent.** AWS code lives in the DynamoDB modules and is imported lazily. `boto3` stays an optional extra.
- **Secrets stay in `.env`.** Never put API keys in code, notebooks, command arguments or test fixtures.

## Scope

- AWS work moves one bounded milestone at a time, in the order of `docs/aws-architecture.md` → Delivery sequence. Build only the milestone the task names. Do not start later ones (CDK, EventBridge, deployment workflows, scheduled ingestion) early.
- `src/asof/dynamodb.py` is already over 1,000 lines. Put new DynamoDB behavior in a focused module, not in that file.
- Update `README.md` (Project status) and `docs/release-history.md` when behavior a user can see changes.

## Working rules

- Read the code you are changing and its tests before editing. Prefer existing helpers to new ones.
- For a bug, write the failing test first, watch it fail, then fix it.
- Done means the full suite passes with the DynamoDB extras installed. Report the command you ran and its result. "It should work" is not done.
- Keep changes small. Do not refactor, rename or reformat code the task does not touch.

## Git and pull requests

- Small commits with ordinary, human-written messages. One reviewable pull request per milestone.
- Anastasia Galkova is the sole author and committer of every commit. Do not sign commits (no SSH or GPG signatures).
- Commits, files and PR descriptions carry no tool metadata: no `Co-Authored-By` trailers, session links, "generated with" footers or AI tool names. If the environment adds attribution that cannot be turned off, stop before pushing and say so.
- Never merge pull requests, force-push, push to `main` or deploy anything. Anastasia does those.

## Review

Before asking for review, run `/thermo-nuclear-code-quality-review` on the branch and `/blast-radius` on anything that touches a store, publication or cleanup.
