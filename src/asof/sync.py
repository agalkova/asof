"""Fetch and store revision histories in batches."""

from __future__ import annotations

from dataclasses import dataclass

from .fred import get_revision_histories_chunked
from .models import RevisionHistory
from .storage import RevisionStore


@dataclass(frozen=True)
class SyncResult:
    series_id: str
    observation_start: str
    observation_end: str
    observations: int
    vintages: int


class RevisionConflictError(RuntimeError):
    """Raised when fetched data would remove or change a stored vintage."""


def _check_revision_history(
    stored: RevisionHistory | None,
    fetched: RevisionHistory,
) -> None:
    """Require a refresh to preserve every vintage already stored."""

    if stored is None:
        return
    fetched_by_date = {
        revision.vintage_date: revision.value for revision in fetched.revisions
    }
    for revision in stored.revisions:
        if fetched_by_date.get(revision.vintage_date) != revision.value:
            raise RevisionConflictError(
                "Fetched history would remove or change a stored vintage"
            )


def sync_revision_histories(
    *,
    store: RevisionStore,
    series_id: str,
    observation_start: str,
    observation_end: str,
    api_key: str,
) -> SyncResult:
    """Fetch and store a range, publishing each observation atomically."""

    histories = get_revision_histories_chunked(
        series_id=series_id,
        observation_start=observation_start,
        observation_end=observation_end,
        api_key=api_key,
    )
    for history in histories:
        stored = store.load_history(
            series_id=history.series_id,
            observation_date=history.observation_date,
        )
        _check_revision_history(stored, history)
    vintages = store.save_histories(histories)
    return SyncResult(
        series_id=series_id,
        observation_start=observation_start,
        observation_end=observation_end,
        observations=len(histories),
        vintages=vintages,
    )
