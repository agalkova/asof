"""Small FRED/ALFRED client for point-in-time observations."""

from __future__ import annotations

from dataclasses import dataclass

import requests

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class FredApiError(RuntimeError):
    """A sanitized FRED error that never includes the API-key-bearing URL."""


@dataclass(frozen=True)
class FredObservation:
    """One FRED observation, measured in the series' native units."""

    series_id: str
    observation_date: str
    value: float
    realtime_start: str
    realtime_end: str


@dataclass(frozen=True)
class VintageValue:
    """The value published for an observation on one vintage date."""

    vintage_date: str
    value: float


@dataclass(frozen=True)
class RevisionHistory:
    """Every published value change for one observation."""

    series_id: str
    observation_date: str
    revisions: tuple[VintageValue, ...]

    @property
    def initial(self) -> VintageValue:
        return self.revisions[0]

    @property
    def current(self) -> VintageValue:
        return self.revisions[-1]

    @property
    def total_revision(self) -> float:
        """Latest published value minus the initial release value."""

        return self.current.value - self.initial.value


@dataclass(frozen=True)
class RevisionComparison:
    """Historical and current values for the same observation."""

    historical: FredObservation
    current: FredObservation

    @property
    def revision(self) -> float:
        """Current value minus the historical value."""

        return self.current.value - self.historical.value


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

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": observation_date,
        "observation_end": observation_date,
        "realtime_start": observation_date,
        "realtime_end": "9999-12-31",
        "output_type": 3,
    }
    payload = _request_json(params=params, timeout=timeout)
    observations = payload.get("observations", [])
    if len(observations) != 1:
        raise ValueError(
            f"Expected one {series_id} revision row for {observation_date}; "
            f"received {len(observations)}"
        )

    prefix = f"{series_id}_"
    revisions = []
    for key, value in observations[0].items():
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
