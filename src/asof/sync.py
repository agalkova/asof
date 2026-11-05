"""Fetch and store revision histories in batches."""

from __future__ import annotations

from dataclasses import dataclass

from .fred import get_revision_histories_chunked
from .storage import RevisionStore


@dataclass(frozen=True)
class SyncResult:
    series_id: str
    observation_start: str
    observation_end: str
    observations: int
    vintages: int


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
    vintages = store.save_histories(histories)
    return SyncResult(
        series_id=series_id,
        observation_start=observation_start,
        observation_end=observation_end,
        observations=len(histories),
        vintages=vintages,
    )
