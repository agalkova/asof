"""Read-only HTTP API for stored revision histories."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel

from ._version import __version__
from .models import SERIES_ID_PATTERN
from .storage import RevisionStore


SeriesId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=120,
        pattern=rf"^{SERIES_ID_PATTERN}$",
        description="FRED series ID",
    ),
]


class ObservationDatesResponse(BaseModel):
    series_id: str
    observations: tuple[date, ...]


class VintageResponse(BaseModel):
    vintage_date: date
    value: float


class RevisionHistoryResponse(BaseModel):
    series_id: str
    observation_date: date
    initial_value: float
    latest_value: float
    total_revision: float
    revisions: tuple[VintageResponse, ...]


class AsOfValueResponse(BaseModel):
    series_id: str
    observation_date: date
    as_of: date
    vintage_date: date
    value: float


def create_app(store: RevisionStore, *, close_store: bool = False) -> FastAPI:
    """Create the API around an existing revision store."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if close_store:
            store.close()

    app = FastAPI(title="AsOf API", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        if not store.is_ready():
            raise HTTPException(status_code=503, detail="Storage is unavailable")
        return {"status": "ok"}

    @app.get(
        "/series/{series_id}/observations",
        response_model=ObservationDatesResponse,
    )
    def list_observations(series_id: SeriesId) -> ObservationDatesResponse:
        observations = store.list_observations(series_id=series_id)
        return ObservationDatesResponse(
            series_id=series_id,
            observations=tuple(date.fromisoformat(value) for value in observations),
        )

    @app.get(
        "/series/{series_id}/observations/{observation_date}",
        response_model=RevisionHistoryResponse,
    )
    def get_revision_history(
        series_id: SeriesId,
        observation_date: date,
    ) -> RevisionHistoryResponse:
        history = store.load_history(
            series_id=series_id,
            observation_date=observation_date.isoformat(),
        )
        if history is None:
            raise HTTPException(
                status_code=404,
                detail=f"No stored history for {series_id} on {observation_date}",
            )
        return RevisionHistoryResponse(
            series_id=history.series_id,
            observation_date=date.fromisoformat(history.observation_date),
            initial_value=history.initial.value,
            latest_value=history.current.value,
            total_revision=history.total_revision,
            revisions=tuple(
                VintageResponse(
                    vintage_date=date.fromisoformat(revision.vintage_date),
                    value=revision.value,
                )
                for revision in history.revisions
            ),
        )

    @app.get(
        "/series/{series_id}/observations/{observation_date}/value",
        response_model=AsOfValueResponse,
    )
    def get_value_as_of(
        series_id: SeriesId,
        observation_date: date,
        as_of: date,
    ) -> AsOfValueResponse:
        lookup = store.lookup_value_as_of(
            series_id=series_id,
            observation_date=observation_date.isoformat(),
            as_of_date=as_of.isoformat(),
        )
        if not lookup.observation_exists:
            raise HTTPException(
                status_code=404,
                detail=f"No stored history for {series_id} on {observation_date}",
            )
        if lookup.revision is None:
            raise HTTPException(
                status_code=404,
                detail=f"{series_id} for {observation_date} was not published by {as_of}",
            )
        return AsOfValueResponse(
            series_id=series_id,
            observation_date=observation_date,
            as_of=as_of,
            vintage_date=date.fromisoformat(lookup.revision.vintage_date),
            value=lookup.revision.value,
        )

    return app
