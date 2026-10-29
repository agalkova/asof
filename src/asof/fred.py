"""Small FRED/ALFRED client for point-in-time observations."""

from __future__ import annotations

import requests

from .models import FredObservation, RevisionComparison, RevisionHistory, VintageValue

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
MAX_VINTAGE_DATES_PER_RESPONSE = 2_000


class FredApiError(RuntimeError):
    """A sanitized FRED error that never includes the API-key-bearing URL."""


def _request_json(*, params: dict[str, object], timeout: float) -> dict:
    """Make a FRED request without leaking its API-key-bearing URL in errors."""

    response = None
    try:
        response = requests.get(BASE_URL, params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        status = getattr(response, "status_code", None)
        detail = f" with HTTP status {status}" if status is not None else ""
        raise FredApiError(f"FRED request failed{detail}") from None

    try:
        payload = response.json()
    except ValueError:
        raise FredApiError("FRED returned an invalid JSON response") from None
    if not isinstance(payload, dict):
        raise FredApiError("FRED returned an unexpected JSON response")
    return payload


def get_observation(
    *,
    series_id: str,
    observation_date: str,
    api_key: str,
    as_of_date: str | None = None,
    timeout: float = 30,
) -> FredObservation:
    """Fetch one observation, optionally as it was known on ``as_of_date``."""

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": observation_date,
        "observation_end": observation_date,
    }
    if as_of_date is not None:
        params.update(realtime_start=as_of_date, realtime_end=as_of_date)

    payload = _request_json(params=params, timeout=timeout)
    observations = payload.get("observations", [])

    if len(observations) != 1:
        raise ValueError(
            f"Expected one {series_id} observation for {observation_date}; "
            f"received {len(observations)}"
        )

    observation = observations[0]
    if observation["value"] == ".":
        raise ValueError(f"{series_id} has no value for {observation_date}")

    return FredObservation(
        series_id=series_id,
        observation_date=observation["date"],
        value=float(observation["value"]),
        realtime_start=observation["realtime_start"],
        realtime_end=observation["realtime_end"],
    )


def get_revision_history(
    *,
    series_id: str,
    observation_date: str,
    api_key: str,
    timeout: float = 30,
) -> RevisionHistory:
    """Fetch each date when one observation's published value changed."""

    histories = get_revision_histories(
        series_id=series_id,
        observation_start=observation_date,
        observation_end=observation_date,
        api_key=api_key,
        timeout=timeout,
    )
    if len(histories) != 1:
        raise ValueError(
            f"Expected one {series_id} revision row for {observation_date}; "
            f"received {len(histories)}"
        )
    return histories[0]


def get_revision_histories(
    *,
    series_id: str,
    observation_start: str,
    observation_end: str,
    api_key: str,
    timeout: float = 30,
) -> tuple[RevisionHistory, ...]:
    """Fetch revision histories for a compact, inclusive observation range.

    ALFRED limits this response format to 2,000 vintage dates. Potentially
    truncated responses are rejected instead of being stored as complete data.
    """

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": observation_start,
        "observation_end": observation_end,
        "realtime_start": observation_start,
        "realtime_end": "9999-12-31",
        "output_type": 3,
    }
    payload = _request_json(params=params, timeout=timeout)
    observations = payload.get("observations", [])

    vintage_dates = {
        key.removeprefix(f"{series_id}_")
        for row in observations
        for key in row
        if key.startswith(f"{series_id}_")
    }
    if len(vintage_dates) >= MAX_VINTAGE_DATES_PER_RESPONSE:
        raise ValueError(
            "ALFRED returned its 2,000-vintage limit; request a smaller range "
            "to avoid storing an incomplete history"
        )

    histories = tuple(_parse_revision_row(series_id=series_id, row=row) for row in observations)
    return tuple(sorted(histories, key=lambda history: history.observation_date))


def _parse_revision_row(*, series_id: str, row: dict) -> RevisionHistory:
    observation_date = row.get("date")
    if not isinstance(observation_date, str):
        raise ValueError(f"{series_id} revision row is missing its observation date")

    prefix = f"{series_id}_"
    revisions = []
    for key, value in row.items():
        vintage = key.removeprefix(prefix)
        if not key.startswith(prefix) or len(vintage) != 8 or not vintage.isdigit():
            continue
        if value == ".":
            continue
        revisions.append(
            VintageValue(
                vintage_date=f"{vintage[:4]}-{vintage[4:6]}-{vintage[6:]}",
                value=float(value),
            )
        )

    revisions.sort(key=lambda item: item.vintage_date)
    if not revisions:
        raise ValueError(f"{series_id} has no revision history for {observation_date}")

    return RevisionHistory(
        series_id=series_id,
        observation_date=observation_date,
        revisions=tuple(revisions),
    )


def compare_vintages(
    *,
    series_id: str,
    observation_date: str,
    historical_as_of: str,
    api_key: str,
) -> RevisionComparison:
    """Compare a historical vintage with today's currently published value."""

    historical = get_observation(
        series_id=series_id,
        observation_date=observation_date,
        as_of_date=historical_as_of,
        api_key=api_key,
    )
    current = get_observation(
        series_id=series_id,
        observation_date=observation_date,
        api_key=api_key,
    )
    return RevisionComparison(historical=historical, current=current)
