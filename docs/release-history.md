# Release history

## v0.1 — Point-in-time prototype

The first notebook reconstructed August 2008 payroll employment as it was known on September 12, 2008, then compared that value with the current revision.

## v0.2 — Revision histories

The client retrieved every published value for one observation and checked the approach across payrolls, unemployment, CPI, real GDP, and the federal funds rate.

## v0.3 — SQLite storage

Revision histories could be saved locally and loaded again without calling FRED.

## v0.4 — Range sync

The sync path fetched several observation dates in one request and stored the batch in one transaction.

## v0.5 — PostgreSQL storage

A PostgreSQL implementation was added behind the same storage interface as SQLite.

## v0.6 — Read API

A read-only FastAPI service exposed observation lists, revision histories, and point-in-time values.

## v0.7 — Command-line sync

The `asof sync` command loaded a requested date range into the configured database while keeping credentials out of command history.

## v0.8 — Safe request chunking

Long ALFRED requests began splitting automatically at the 2,000-vintage response limit instead of saving incomplete results.

## v0.9 — Deployment preview

The project gained a production container, database initialization, and rolling refresh commands. An initial Render plan was evaluated and retired before deployment when AWS became the portfolio target.

## v0.9.1 — AWS preparation

Documentation now presents the product clearly, records the proposed AWS architecture, and runs the test suite in GitHub Actions. No cloud resources are created in this release.
