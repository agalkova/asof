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


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use a positive whole number") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("use a positive whole number")
    return parsed


def _add_storage_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--sqlite",
        type=Path,
        help="use this SQLite file instead of ASOF_DATABASE_URL or ASOF_DATABASE_PATH",
    )


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
    _add_storage_argument(sync)

    refresh = commands.add_parser(
        "refresh",
        help="fetch and store a rolling window ending today",
    )
    refresh.add_argument("--series", required=True, help="FRED series ID, such as PAYEMS")
    refresh.add_argument(
        "--lookback-months",
        type=_positive_integer,
        default=24,
        help="number of calendar months to refresh (default: 24)",
    )
    _add_storage_argument(refresh)

    init_db = commands.add_parser("init-db", help="create the database schema")
    _add_storage_argument(init_db)
    return parser


def _build_store(sqlite_path: Path | None) -> RevisionStore:
    if sqlite_path is not None:
        return SQLiteRevisionStore(sqlite_path)
    database_url = os.getenv("ASOF_DATABASE_URL")
    if database_url:
        return PostgresRevisionStore(database_url)
    return SQLiteRevisionStore(os.getenv("ASOF_DATABASE_PATH", "data/asof.sqlite3"))


def _lookback_range(today: date, months: int) -> tuple[str, str]:
    month_number = today.year * 12 + today.month - months
    start = date(month_number // 12, month_number % 12 + 1, 1)
    return start.isoformat(), today.isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == "init-db":
        try:
            with closing(_build_store(args.sqlite)):
                pass
        except (PostgresConnectionError, ValueError) as error:
            parser.exit(1, f"asof: {error}\n")
        print("Database schema is ready.")
        return 0

    if args.command == "sync":
        if args.start > args.end:
            parser.error("--start must be on or before --end")
        observation_start, observation_end = args.start, args.end
    else:
        try:
            observation_start, observation_end = _lookback_range(
                date.today(),
                args.lookback_months,
            )
        except ValueError:
            parser.error("--lookback-months reaches before year 1")

    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        parser.error("FRED_API_KEY is not set")

    try:
        with closing(_build_store(args.sqlite)) as store:
            result = sync_revision_histories(
                store=store,
                series_id=args.series,
                observation_start=observation_start,
                observation_end=observation_end,
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
