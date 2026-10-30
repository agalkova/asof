"""Command-line entry point for loading revision data."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .fred import FredApiError
from .postgres import PostgresConnectionError, PostgresRevisionStore
from .storage import RevisionStore, SQLiteRevisionStore
from .sync import sync_revision_histories


def _iso_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD format") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD format")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asof",
        description="Load point-in-time economic data from ALFRED.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="fetch and store a revision range")
    sync.add_argument("--series", required=True, help="FRED series ID, such as PAYEMS")
    sync.add_argument("--start", required=True, type=_iso_date, help="first observation date")
    sync.add_argument("--end", required=True, type=_iso_date, help="last observation date")
    sync.add_argument(
        "--sqlite",
        type=Path,
        help="use this SQLite file instead of ASOF_DATABASE_URL or ASOF_DATABASE_PATH",
    )
    return parser


def _build_store(sqlite_path: Path | None) -> RevisionStore:
    if sqlite_path is not None:
        return SQLiteRevisionStore(sqlite_path)
    database_url = os.getenv("ASOF_DATABASE_URL")
    if database_url:
        return PostgresRevisionStore(database_url)
    return SQLiteRevisionStore(os.getenv("ASOF_DATABASE_PATH", "data/asof.sqlite3"))


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args(argv)

    if args.start > args.end:
        parser.error("--start must be on or before --end")
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        parser.error("FRED_API_KEY is not set")

    try:
        with closing(_build_store(args.sqlite)) as store:
            result = sync_revision_histories(
                store=store,
                series_id=args.series,
                observation_start=args.start,
                observation_end=args.end,
                api_key=api_key,
            )
    except (FredApiError, PostgresConnectionError, ValueError) as error:
        parser.exit(1, f"asof: {error}\n")

    vintage_label = "vintage" if result.vintages == 1 else "vintages"
    observation_label = "observation" if result.observations == 1 else "observations"
    print(
        f"Stored {result.vintages} {vintage_label} across {result.observations} "
        f"{result.series_id} {observation_label} ({result.observation_start} through "
        f"{result.observation_end})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
