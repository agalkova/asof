"""Domain models shared by data sources and storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite


def _validate_iso_date(value: str, field_name: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format: {value!r}") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format: {value!r}")


@dataclass(frozen=True)
class FredObservation:
    """One point-in-time FRED observation in the series' native units."""

    series_id: str
    observation_date: str
    value: float
    realtime_start: str
    realtime_end: str

    def __post_init__(self) -> None:
        if not self.series_id:
            raise ValueError("series_id must not be empty")
        _validate_iso_date(self.observation_date, "observation_date")
        _validate_iso_date(self.realtime_start, "realtime_start")
        _validate_iso_date(self.realtime_end, "realtime_end")
        if not isfinite(self.value):
            raise ValueError("value must be finite")


@dataclass(frozen=True)
class VintageValue:
    """The value published for an observation on one vintage date."""

    vintage_date: str
    value: float

    def __post_init__(self) -> None:
        _validate_iso_date(self.vintage_date, "vintage_date")
        if not isfinite(self.value):
            raise ValueError("value must be finite")


@dataclass(frozen=True)
class RevisionHistory:
    """Every published value change for one series observation."""

    series_id: str
    observation_date: str
    revisions: tuple[VintageValue, ...]

    def __post_init__(self) -> None:
        if not self.series_id:
            raise ValueError("series_id must not be empty")
        _validate_iso_date(self.observation_date, "observation_date")
        if not self.revisions:
            raise ValueError("revisions must not be empty")
        dates = [revision.vintage_date for revision in self.revisions]
        if dates != sorted(set(dates)):
            raise ValueError("revisions must have unique vintage dates in ascending order")

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
