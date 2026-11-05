"""SQLite persistence for point-in-time revision histories."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Protocol

from .models import AsOfLookup, RevisionHistory, VintageValue

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    series_id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS observations (
    series_id TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    PRIMARY KEY (series_id, observation_date),
    FOREIGN KEY (series_id) REFERENCES series (series_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vintages (
    series_id TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    vintage_date TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (series_id, observation_date, vintage_date),
    FOREIGN KEY (series_id, observation_date)
        REFERENCES observations (series_id, observation_date) ON DELETE CASCADE
);
"""


class RevisionStore(Protocol):
    """Storage behavior shared by synchronization and read APIs."""

    def save_histories(self, histories: Iterable[RevisionHistory]) -> int:
        """Store several histories and return the number of vintages written."""
        ...

    def load_history(self, *, series_id: str, observation_date: str) -> RevisionHistory | None:
        """Load one stored history."""
        ...

    def lookup_value_as_of(
        self,
        *,
        series_id: str,
        observation_date: str,
        as_of_date: str,
    ) -> AsOfLookup:
        """Find the latest vintage available on a date without loading its history."""
        ...

    def list_observations(self, *, series_id: str) -> tuple[str, ...]:
        """List stored observation dates for a series."""
        ...

    def close(self) -> None:
        """Close the underlying database connection."""
        ...

    def is_ready(self) -> bool:
        """Return whether the store can serve reads."""
        ...


class SQLiteRevisionStore:
    """Store revision histories in a local SQLite database."""

    def __init__(self, database: str | Path, *, read_only: bool = False) -> None:
        self._lock = RLock()
        if read_only:
            if database == ":memory:":
                raise ValueError("an in-memory SQLite database cannot be opened read-only")
            uri = f"{Path(database).resolve().as_uri()}?mode=ro"
            self._connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
            )
        else:
            if database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self._connection.executescript(SCHEMA)

    def save_history(self, history: RevisionHistory) -> int:
        """Replace the stored history for one observation and return its row count."""

        return self.save_histories((history,))

    def save_histories(self, histories: Iterable[RevisionHistory]) -> int:
        """Replace several histories in one transaction and return the vintage count."""

        histories = tuple(histories)
        with self._lock:
            with self._connection:
                for history in histories:
                    self._replace_history(history)
        return sum(len(history.revisions) for history in histories)

    def _replace_history(self, history: RevisionHistory) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO series (series_id) VALUES (?)",
            (history.series_id,),
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO observations (series_id, observation_date)
            VALUES (?, ?)
            """,
            (history.series_id, history.observation_date),
        )
        self._connection.execute(
            """
            DELETE FROM vintages
            WHERE series_id = ? AND observation_date = ?
            """,
            (history.series_id, history.observation_date),
        )
        self._connection.executemany(
            """
            INSERT INTO vintages (series_id, observation_date, vintage_date, value)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    history.series_id,
                    history.observation_date,
                    revision.vintage_date,
                    revision.value,
                )
                for revision in history.revisions
            ],
        )

    def load_history(self, *, series_id: str, observation_date: str) -> RevisionHistory | None:
        """Load one history, or return ``None`` when it is not stored."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT vintage_date, value
                FROM vintages
                WHERE series_id = ? AND observation_date = ?
                ORDER BY vintage_date
                """,
                (series_id, observation_date),
            ).fetchall()
        if not rows:
            return None
        return RevisionHistory(
            series_id=series_id,
            observation_date=observation_date,
            revisions=tuple(
                VintageValue(vintage_date=row["vintage_date"], value=row["value"])
                for row in rows
            ),
        )

    def lookup_value_as_of(
        self,
        *,
        series_id: str,
        observation_date: str,
        as_of_date: str,
    ) -> AsOfLookup:
        """Find one value with a bounded query, preserving missing-state details."""

        with self._lock:
            row = self._connection.execute(
                """
                SELECT v.vintage_date, v.value
                FROM observations AS o
                LEFT JOIN vintages AS v
                  ON v.series_id = o.series_id
                 AND v.observation_date = o.observation_date
                 AND v.vintage_date <= ?
                WHERE o.series_id = ? AND o.observation_date = ?
                ORDER BY v.vintage_date DESC
                LIMIT 1
                """,
                (as_of_date, series_id, observation_date),
            ).fetchone()
        if row is None:
            return AsOfLookup(observation_exists=False, revision=None)
        revision = None
        if row["vintage_date"] is not None:
            revision = VintageValue(vintage_date=row["vintage_date"], value=row["value"])
        return AsOfLookup(observation_exists=True, revision=revision)

    def list_observations(self, *, series_id: str) -> tuple[str, ...]:
        """Return stored observation dates for a series in ascending order."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT observation_date
                FROM observations
                WHERE series_id = ?
                ORDER BY observation_date
                """,
                (series_id,),
            ).fetchall()
        return tuple(row["observation_date"] for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def is_ready(self) -> bool:
        with self._lock:
            try:
                self._connection.execute("SELECT 1 FROM series LIMIT 1").fetchone()
            except sqlite3.Error:
                return False
        return True

    def __enter__(self) -> SQLiteRevisionStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
