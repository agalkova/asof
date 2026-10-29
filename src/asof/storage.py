"""SQLite persistence for point-in-time revision histories."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType

from .models import RevisionHistory, VintageValue

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


class SQLiteRevisionStore:
    """Store revision histories in a local SQLite database."""

    def __init__(self, database: str | Path) -> None:
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(SCHEMA)

    def save_history(self, history: RevisionHistory) -> int:
        """Replace the stored history for one observation and return its row count."""

        return self.save_histories((history,))

    def save_histories(self, histories: Iterable[RevisionHistory]) -> int:
        """Replace several histories in one transaction and return the vintage count."""

        histories = tuple(histories)
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

    def list_observations(self, *, series_id: str) -> tuple[str, ...]:
        """Return stored observation dates for a series in ascending order."""

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
        self._connection.close()

    def __enter__(self) -> SQLiteRevisionStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
