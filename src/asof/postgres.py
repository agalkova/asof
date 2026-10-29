"""PostgreSQL persistence for point-in-time revision histories."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from threading import RLock
from types import TracebackType
from typing import Any

from .models import RevisionHistory, VintageValue

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS series (
        series_id TEXT PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        series_id TEXT NOT NULL,
        observation_date DATE NOT NULL,
        PRIMARY KEY (series_id, observation_date),
        FOREIGN KEY (series_id) REFERENCES series (series_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vintages (
        series_id TEXT NOT NULL,
        observation_date DATE NOT NULL,
        vintage_date DATE NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (series_id, observation_date, vintage_date),
        FOREIGN KEY (series_id, observation_date)
            REFERENCES observations (series_id, observation_date) ON DELETE CASCADE
    )
    """,
)


def _connect(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            'PostgreSQL support is optional. Install it with pip install -e ".[postgres]".'
        ) from error
    return psycopg.connect(dsn)


def _iso_date(value: str | date) -> str:
    return value.isoformat() if isinstance(value, date) else value


class PostgresRevisionStore:
    """Store revision histories in PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[str], Any] | None = None,
        read_only: bool = False,
    ) -> None:
        connect = connection_factory or _connect
        self._lock = RLock()
        self._connection = connect(dsn)
        if not read_only:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    for statement in SCHEMA:
                        cursor.execute(statement)

    def save_history(self, history: RevisionHistory) -> int:
        """Replace the stored history for one observation and return its row count."""

        return self.save_histories((history,))

    def save_histories(self, histories: Iterable[RevisionHistory]) -> int:
        """Replace several histories in one transaction and return the vintage count."""

        histories = tuple(histories)
        with self._lock:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    for history in histories:
                        self._replace_history(cursor, history)
        return sum(len(history.revisions) for history in histories)

    @staticmethod
    def _replace_history(cursor: Any, history: RevisionHistory) -> None:
        cursor.execute(
            "INSERT INTO series (series_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (history.series_id,),
        )
        cursor.execute(
            """
            INSERT INTO observations (series_id, observation_date)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (history.series_id, history.observation_date),
        )
        cursor.execute(
            """
            DELETE FROM vintages
            WHERE series_id = %s AND observation_date = %s
            """,
            (history.series_id, history.observation_date),
        )
        cursor.executemany(
            """
            INSERT INTO vintages (series_id, observation_date, vintage_date, value)
            VALUES (%s, %s, %s, %s)
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
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT vintage_date, value
                        FROM vintages
                        WHERE series_id = %s AND observation_date = %s
                        ORDER BY vintage_date
                        """,
                        (series_id, observation_date),
                    )
                    rows = cursor.fetchall()
        if not rows:
            return None
        return RevisionHistory(
            series_id=series_id,
            observation_date=observation_date,
            revisions=tuple(
                VintageValue(vintage_date=_iso_date(row[0]), value=float(row[1]))
                for row in rows
            ),
        )

    def list_observations(self, *, series_id: str) -> tuple[str, ...]:
        """Return stored observation dates for a series in ascending order."""

        with self._lock:
            with self._connection.transaction():
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT observation_date
                        FROM observations
                        WHERE series_id = %s
                        ORDER BY observation_date
                        """,
                        (series_id,),
                    )
                    rows = cursor.fetchall()
        return tuple(_iso_date(row[0]) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def is_ready(self) -> bool:
        with self._lock:
            try:
                with self._connection.transaction():
                    with self._connection.cursor() as cursor:
                        cursor.execute("SELECT 1 FROM series LIMIT 1")
                        cursor.fetchone()
            except Exception:
                return False
        return True

    def __enter__(self) -> PostgresRevisionStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
