"""DynamoDB persistence for point-in-time revision histories.

Each observation has a manifest item that names its active generation. A
replacement history is written under a new, immutable generation partition and
becomes visible only when a conditional transaction moves the manifest pointer.
Readers therefore see either the complete previous history or the complete new
one.

Item layout (partition key ``pk``, sort key ``sk``, both strings):

=========  ======================================  ======================
Item       pk                                      sk
=========  ======================================  ======================
Manifest   ``SERIES#<id>``                         ``OBS#<date>``
Vintage    ``SERIES#<id>#OBS#<date>#GEN#<gen>``    ``VINTAGE#<date>``
Cleanup    ``SERIES#<id>``                         ``RETIRE#<date>#<gen>``
=========  ======================================  ======================

Every generation has a cleanup record whose ``lifecycle`` attribute moves
through these states:

``staged``
    Written before any vintage item. The generation is not visible to readers.
``active``
    Set by the transaction that points the manifest at the generation.
``retired``
    Set on the previous generation by the transaction that replaces it.
``deleting``
    Set by a cleanup claim. The generation can never become active again.

Publication requires ``staged``; a cleanup claim requires ``staged`` or
``retired``, a passed ``delete_after`` time, and a manifest that does not point
at the generation. Both are transactions that write the same cleanup record,
so at most one of them can succeed for a generation.

A claim also records ``claimed_at``. ``sweep_generation`` deletes the items of
a ``deleting`` generation and ``finalize_generation_cleanup`` removes its
cleanup record; ``asof.dynamodb_cleanup`` decides when to call them.

The store never creates, alters, or scans the table. Saving and reading never
delete anything, and only a generation in the ``deleting`` state is deleted.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from types import TracebackType
from typing import Any
from uuid import uuid4

from .models import (
    AsOfLookup,
    RevisionHistory,
    VintageValue,
    validate_iso_date,
    validate_series_id,
)

# A generation that is not active may be removed once this much time has passed
# since it was staged or replaced. It must stay longer than any single read.
GENERATION_GRACE_PERIOD = timedelta(minutes=30)

_BATCH_WRITE_LIMIT = 25
_TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9._]{1,128}$")
_TRANSACTION_CANCELED = "TransactionCanceledException"
_GENERATION_ID = re.compile(r"^[0-9a-f]{32}$")

_LIFECYCLE_NAMES = {"#lifecycle": "lifecycle"}
_STAGED = {"S": "staged"}
_ACTIVE = {"S": "active"}
_RETIRED = {"S": "retired"}
_DELETING = {"S": "deleting"}

_LIFECYCLES = frozenset({"staged", "active", "retired", "deleting"})
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_OBSERVATION_PREFIX = "OBS#"
_VINTAGE_PREFIX = "VINTAGE#"
_CLEANUP_PREFIX = "RETIRE#"
_READINESS_KEY = {"pk": {"S": "ASOF#READINESS"}, "sk": {"S": "PROBE"}}

# DynamoDB numbers hold 38 significant digits between 1E-130 and 9.99...E+125.
_MAX_MAGNITUDE = Decimal("9.9999999999999999999999999999999999999E+125")
_MIN_MAGNITUDE = Decimal("1E-130")
_SDK_RETRY_BACKOFF_CAP_SECONDS = 20
_SDK_RETRY_AFTER_ALLOWANCE_SECONDS = 5


class DynamoDBStoreError(RuntimeError):
    """Base class for DynamoDB storage failures."""


class DynamoDBConfigurationError(DynamoDBStoreError):
    """Raised when the store or its client is configured incorrectly."""


class DynamoDBRequestError(DynamoDBStoreError):
    """Raised when a DynamoDB request fails.

    Only the operation name and the service error code are kept. The original
    message may contain resource names, endpoints, or request content.
    """

    def __init__(self, operation: str, code: str) -> None:
        super().__init__(f"DynamoDB {operation} failed ({code})")
        self.operation = operation
        self.code = code


class DynamoDBRetryExhaustedError(DynamoDBStoreError):
    """Raised when DynamoDB keeps deferring work after every allowed attempt.

    ``unprocessed`` is the number of batched requests DynamoDB had still not
    accepted when a batch write gave up.
    """

    def __init__(self, message: str, *, unprocessed: int = 0) -> None:
        super().__init__(message)
        self.unprocessed = unprocessed


class DynamoDBConcurrencyError(DynamoDBStoreError):
    """Raised when another writer published a different history first."""


class DynamoDBDataError(DynamoDBStoreError):
    """Raised when stored items do not have the expected shape."""


class DynamoDBSweepError(DynamoDBStoreError):
    """Raised when a sweep stops on a failure after it may have deleted items.

    ``acknowledged`` counts the delete requests DynamoDB accepted before the
    failure. The generation is still claimed and can be swept again.
    """

    def __init__(self, message: str, *, acknowledged: int) -> None:
        super().__init__(message)
        self.acknowledged = acknowledged


class DynamoDBValueError(DynamoDBStoreError, ValueError):
    """Raised when a value cannot be represented as a DynamoDB number."""


@dataclass(frozen=True)
class _Manifest:
    generation: str
    vintage_count: int


@dataclass(frozen=True)
class _Replacement:
    series_id: str
    observation_date: str
    vintages: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CleanupRecord:
    """The cleanup record of one generation.

    ``delete_after`` is set while the record is ``staged`` or ``retired``, and
    ``claimed_at`` once it is ``deleting``.
    """

    series_id: str
    observation_date: str
    generation: str
    lifecycle: str
    delete_after: datetime | None
    claimed_at: datetime | None

    @property
    def sort_key(self) -> str:
        return f"{_CLEANUP_PREFIX}{self.observation_date}#{self.generation}"


@dataclass(frozen=True)
class SweepResult:
    """What one sweep of a claimed generation did.

    ``acknowledged`` counts delete requests that DynamoDB accepted: sent in a
    batch and not returned as unprocessed. ``complete`` is true when the sweep
    read the generation's partition to the end, so that every item it saw has
    an acknowledged delete. It is false when the sweep stopped at ``max_items``
    with at least one item left.
    """

    acknowledged: int
    complete: bool


@dataclass(frozen=True)
class MalformedCleanupRecord:
    """A cleanup record that cannot be interpreted and must be left alone."""

    series_id: str
    sort_key: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _to_number(value: float) -> str:
    """Convert a finite float to an exact DynamoDB number string.

    Values are read back as floats, so an integer is accepted only when a
    float holds it exactly.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DynamoDBValueError("value must be a number")
    try:
        as_float = float(value)
    except OverflowError:
        raise DynamoDBValueError("value is outside the range DynamoDB can store") from None
    if not isfinite(as_float):
        raise DynamoDBValueError("value must be finite")
    if as_float != value:
        raise DynamoDBValueError("integer value cannot be stored as a float without changing it")
    if as_float == 0:
        return "0"
    # repr() is the shortest string that round-trips, at most 17 digits.
    number = Decimal(repr(as_float))
    if not _MIN_MAGNITUDE <= abs(number) <= _MAX_MAGNITUDE:
        raise DynamoDBValueError("value is outside the range DynamoDB can store")
    return str(number)


def _string(item: Mapping[str, Any], name: str) -> str:
    attribute = item.get(name)
    if not isinstance(attribute, Mapping) or not isinstance(attribute.get("S"), str):
        raise DynamoDBDataError(f"Stored DynamoDB item has no valid {name!r} string")
    return attribute["S"]


def _number_text(item: Mapping[str, Any], name: str) -> str:
    attribute = item.get(name)
    if not isinstance(attribute, Mapping) or not isinstance(attribute.get("N"), str):
        raise DynamoDBDataError(f"Stored DynamoDB item has no valid {name!r} number")
    return attribute["N"]


def _parse_manifest(item: Mapping[str, Any]) -> _Manifest:
    generation = _string(item, "active_generation")
    if not generation:
        raise DynamoDBDataError("Stored manifest has an empty active generation")
    try:
        vintage_count = int(_number_text(item, "vintage_count"))
    except ValueError:
        raise DynamoDBDataError("Stored manifest has an invalid vintage count") from None
    if vintage_count < 1:
        raise DynamoDBDataError("Stored manifest has an invalid vintage count")
    return _Manifest(generation=generation, vintage_count=vintage_count)


def _parse_vintage(item: Mapping[str, Any]) -> VintageValue:
    sort_key = _string(item, "sk")
    if not sort_key.startswith(_VINTAGE_PREFIX):
        raise DynamoDBDataError("Stored vintage item has an unexpected sort key")
    try:
        value = float(_number_text(item, "value"))
        return VintageValue(
            vintage_date=sort_key[len(_VINTAGE_PREFIX) :],
            value=value,
        )
    except (ValueError, OverflowError):
        raise DynamoDBDataError("Stored vintage item has an invalid date or value") from None


def _parse_timestamp(item: Mapping[str, Any], name: str) -> datetime:
    try:
        parsed = datetime.strptime(_string(item, name), _TIMESTAMP_FORMAT)
    except ValueError:
        raise DynamoDBDataError(f"Stored DynamoDB item has no valid {name!r} time") from None
    return parsed.replace(tzinfo=timezone.utc)


def _parse_cleanup_record(series_id: str, item: Mapping[str, Any]) -> CleanupRecord:
    parts = _string(item, "sk").split("#")
    if len(parts) != 3 or f"{parts[0]}#" != _CLEANUP_PREFIX:
        raise DynamoDBDataError("Stored cleanup record has an unexpected sort key")
    _, observation_date, generation = parts
    try:
        validate_iso_date(observation_date, "observation_date")
    except ValueError:
        raise DynamoDBDataError("Stored cleanup record has an invalid date") from None
    if not _GENERATION_ID.fullmatch(generation):
        raise DynamoDBDataError("Stored cleanup record has an invalid generation")
    # The record must name the partition its own key implies. Anything else
    # leaves it unclear which items the record tracks.
    expected_partition = _generation_key(series_id, observation_date, generation)
    if _string(item, "generation_key") != expected_partition:
        raise DynamoDBDataError("Stored cleanup record names another generation")
    lifecycle = _string(item, "lifecycle")
    if lifecycle not in _LIFECYCLES:
        raise DynamoDBDataError("Stored cleanup record has an unknown lifecycle")
    return CleanupRecord(
        series_id=series_id,
        observation_date=observation_date,
        generation=generation,
        lifecycle=lifecycle,
        delete_after=(
            _parse_timestamp(item, "delete_after")
            if lifecycle in {"staged", "retired"}
            else None
        ),
        claimed_at=_parse_timestamp(item, "claimed_at") if lifecycle == "deleting" else None,
    )


def _series_key(series_id: str) -> str:
    return f"SERIES#{series_id}"


def _manifest_key(series_id: str, observation_date: str) -> dict[str, Any]:
    return {
        "pk": {"S": _series_key(series_id)},
        "sk": {"S": f"{_OBSERVATION_PREFIX}{observation_date}"},
    }


def _generation_key(series_id: str, observation_date: str, generation: str) -> str:
    return f"{_series_key(series_id)}#OBS#{observation_date}#GEN#{generation}"


def _cleanup_key(series_id: str, observation_date: str, generation: str) -> dict[str, Any]:
    return {
        "pk": {"S": _series_key(series_id)},
        "sk": {"S": f"{_CLEANUP_PREFIX}{observation_date}#{generation}"},
    }


def _prepare(history: RevisionHistory) -> _Replacement:
    if not isinstance(history, RevisionHistory):
        raise TypeError("save_histories accepts RevisionHistory values only")
    vintages = tuple(
        (revision.vintage_date, _to_number(revision.value))
        for revision in history.revisions
    )
    return _Replacement(
        series_id=history.series_id,
        observation_date=history.observation_date,
        vintages=vintages,
    )


def _create_client(
    *,
    region_name: str | None,
    endpoint_url: str | None,
    client_config: Any | None,
) -> Any:
    try:
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError
    except ImportError:
        raise DynamoDBConfigurationError(
            'DynamoDB support is optional. Install it with pip install -e ".[dynamodb]".'
        ) from None
    if client_config is None:
        client_config = Config(
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=5,
            read_timeout=15,
        )
    try:
        return boto3.client(
            "dynamodb",
            region_name=region_name,
            endpoint_url=endpoint_url,
            config=client_config,
        )
    except (BotoCoreError, ValueError):
        raise DynamoDBConfigurationError(
            "Could not configure the DynamoDB client; check the AWS region and endpoint"
        ) from None


def _translate(operation: str, error: Exception) -> DynamoDBRequestError | None:
    """Map an SDK failure to a project error without copying its message."""

    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        details = response.get("Error")
        code = details.get("Code") if isinstance(details, Mapping) else None
        if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
            code = "UnknownError"
        return DynamoDBRequestError(operation, code)
    if type(error).__module__.split(".")[0] in {"botocore", "boto3", "urllib3"}:
        return DynamoDBRequestError(operation, type(error).__name__)
    return None


def _estimate_in_flight_write_allowance(client: Any) -> timedelta:
    """Return a conservative upper bound for one SDK request and its retries."""

    config = getattr(getattr(client, "meta", None), "config", None)
    retries = getattr(config, "retries", None)
    connect_timeout = getattr(config, "connect_timeout", None)
    read_timeout = getattr(config, "read_timeout", None)
    if not isinstance(retries, Mapping) or not all(
        isinstance(value, (int, float)) and value > 0
        for value in (connect_timeout, read_timeout)
    ):
        raise DynamoDBConfigurationError(
            "Could not determine the DynamoDB client's timeout and retry settings; "
            "pass in_flight_write_allowance explicitly"
        )
    attempts = retries.get("total_max_attempts")
    if attempts is None and isinstance(retries.get("max_attempts"), int):
        attempts = retries["max_attempts"] + 1
    if attempts is None and retries.get("mode") in {"legacy", "standard", "adaptive"}:
        # Botocore may omit its implicit retry count from ``meta.config``.
        # Six total attempts is a conservative bound for those defaults.
        attempts = 6
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise DynamoDBConfigurationError(
            "Could not determine the DynamoDB client's retry limit; "
            "pass in_flight_write_allowance explicitly"
        )
    seconds = attempts * (connect_timeout + read_timeout)
    retry_wait = _SDK_RETRY_BACKOFF_CAP_SECONDS + _SDK_RETRY_AFTER_ALLOWANCE_SECONDS
    seconds += max(0, attempts - 1) * retry_wait
    return timedelta(seconds=seconds)


class DynamoDBRevisionStore:
    """Store revision histories in an existing DynamoDB table.

    The table must already exist with a string partition key ``pk`` and a
    string sort key ``sk``. Do not enable DynamoDB TTL on ``delete_after``:
    removing a cleanup record on its own would orphan the generation it tracks.

    Readers call ``GetItem`` and ``Query``. Writers also call ``PutItem``,
    ``BatchWriteItem``, and ``TransactWriteItems``. IAM has no separate action
    for a write transaction: its ``Put``, ``Update``, and ``Delete`` entries
    are authorized as ``dynamodb:PutItem``, ``dynamodb:UpdateItem``, and
    ``dynamodb:DeleteItem``, and a ``ConditionCheck`` as
    ``dynamodb:ConditionCheckItem``. Cleanup uses ``GetItem``, ``Query``,
    ``BatchWriteItem``, and transactions with ``Update``, ``Delete``, and
    ``ConditionCheck`` entries.

    A save fails if staging one history takes longer than
    ``GENERATION_GRACE_PERIOD``. Cleanup relies on that limit: once a staged
    generation is due, its writer has stopped sending items.

    Each observation is replaced atomically. A batch is not: when
    ``save_histories`` fails part-way, the observations published before the
    failure stay published. Saving the same batch again is safe.
    """

    def __init__(
        self,
        table_name: str,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        client: Any | None = None,
        client_config: Any | None = None,
        read_only: bool = False,
        max_attempts: int = 8,
        backoff_base_seconds: float = 0.05,
        backoff_cap_seconds: float = 5.0,
        in_flight_write_allowance: timedelta | None = None,
    ) -> None:
        if not isinstance(table_name, str) or not _TABLE_NAME.fullmatch(table_name):
            raise DynamoDBConfigurationError(
                "table_name must be 3-255 letters, numbers, dots, underscores, or hyphens"
            )
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise DynamoDBConfigurationError("max_attempts must be a positive integer")
        if not 0 <= backoff_base_seconds <= backoff_cap_seconds:
            raise DynamoDBConfigurationError(
                "backoff_base_seconds must be between zero and backoff_cap_seconds"
            )
        if in_flight_write_allowance is not None and (
            not isinstance(in_flight_write_allowance, timedelta)
            or in_flight_write_allowance <= timedelta(0)
        ):
            raise DynamoDBConfigurationError(
                "in_flight_write_allowance must be a positive duration"
            )
        if client is not None and (
            region_name is not None or endpoint_url is not None or client_config is not None
        ):
            raise DynamoDBConfigurationError(
                "Pass either a client or region, endpoint, and client configuration"
            )
        self._table_name = table_name
        self._read_only = read_only
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._closed = False
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else _create_client(
                region_name=region_name,
                endpoint_url=endpoint_url,
                client_config=client_config,
            )
        )
        self._in_flight_write_allowance = (
            in_flight_write_allowance
            if in_flight_write_allowance is not None
            else _estimate_in_flight_write_allowance(self._client)
        )

    @property
    def in_flight_write_allowance(self) -> timedelta:
        """Maximum configured duration of one SDK batch-write call."""

        return self._in_flight_write_allowance

    def save_history(self, history: RevisionHistory) -> int:
        """Replace the stored history for one observation and return its vintage count."""

        return self.save_histories((history,))

    def save_histories(self, histories: Iterable[RevisionHistory]) -> int:
        """Replace each observation history atomically and return the vintage count.

        Observations are published one at a time, in input order. Every input
        is validated before the first write.
        """

        replacements = tuple(_prepare(history) for history in histories)
        self._ensure_writable()
        for replacement in replacements:
            self._replace_history(replacement)
        return sum(len(replacement.vintages) for replacement in replacements)

    def load_history(self, *, series_id: str, observation_date: str) -> RevisionHistory | None:
        """Load one history, or return ``None`` when it is not stored."""

        validate_series_id(series_id)
        validate_iso_date(observation_date, "observation_date")
        manifest = self._read_manifest(series_id, observation_date)
        if manifest is None:
            return None
        revisions = tuple(
            _parse_vintage(item)
            for item in self._generation_items(series_id, observation_date, manifest.generation)
        )
        if len(revisions) != manifest.vintage_count:
            raise DynamoDBDataError("Stored generation does not match its manifest")
        try:
            return RevisionHistory(
                series_id=series_id,
                observation_date=observation_date,
                revisions=revisions,
            )
        except ValueError:
            raise DynamoDBDataError("Stored generation is not a valid history") from None

    def lookup_value_as_of(
        self,
        *,
        series_id: str,
        observation_date: str,
        as_of_date: str,
    ) -> AsOfLookup:
        """Find the latest vintage on or before a date without loading the history."""

        validate_series_id(series_id)
        validate_iso_date(observation_date, "observation_date")
        validate_iso_date(as_of_date, "as_of_date")
        manifest = self._read_manifest(series_id, observation_date)
        if manifest is None:
            return AsOfLookup(observation_exists=False, revision=None)
        partition = {
            "S": _generation_key(series_id, observation_date, manifest.generation)
        }
        newest_available = self._query(
            KeyConditionExpression="pk = :pk AND sk BETWEEN :first AND :last",
            ExpressionAttributeValues={
                ":pk": partition,
                ":first": {"S": _VINTAGE_PREFIX},
                ":last": {"S": f"{_VINTAGE_PREFIX}{as_of_date}"},
            },
            ScanIndexForward=False,
            Limit=1,
        )
        for item in newest_available:
            return AsOfLookup(observation_exists=True, revision=_parse_vintage(item))
        # "Not published yet" is only true if the generation still has
        # vintages; an empty one must not be reported as a late release.
        earliest = self._query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": partition},
            ScanIndexForward=True,
            Limit=1,
        )
        for item in earliest:
            _parse_vintage(item)
            return AsOfLookup(observation_exists=True, revision=None)
        raise DynamoDBDataError("Stored generation does not match its manifest")

    def list_observations(self, *, series_id: str) -> tuple[str, ...]:
        """Return stored observation dates for a series in ascending order."""

        validate_series_id(series_id)
        dates = []
        for item in self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": _series_key(series_id)},
                ":prefix": {"S": _OBSERVATION_PREFIX},
            },
            ProjectionExpression="sk",
            ScanIndexForward=True,
        ):
            sort_key = _string(item, "sk")
            try:
                dates.append(
                    validate_iso_date(
                        sort_key.removeprefix(_OBSERVATION_PREFIX),
                        "observation_date",
                    )
                )
            except ValueError:
                raise DynamoDBDataError("Stored manifest has an invalid observation date") from None
        return tuple(dates)

    def claim_generation_for_cleanup(
        self,
        *,
        series_id: str,
        observation_date: str,
        generation: str,
        now: datetime | None = None,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> bool:
        """Atomically reserve a due, inactive generation for deletion.

        Returns ``True`` when the claim succeeded. From then on the generation
        can never be published, so its items may be deleted. Returns ``False``
        when the generation is active, not yet due, already claimed, or
        unknown, or when DynamoDB cancelled the transaction for another reason.

        The claim time is stored as ``claimed_at``. This only claims: it does
        not find due generations or delete anything.
        """

        self._check_generation(series_id, observation_date, generation)
        claimed_at = {"S": _timestamp(now or _utc_now())}
        claim = {
            "TableName": self._table_name,
            "Key": _cleanup_key(series_id, observation_date, generation),
            "UpdateExpression": "SET #lifecycle = :deleting, claimed_at = :now",
            "ConditionExpression": (
                "#lifecycle IN (:staged, :retired) AND delete_after <= :now"
            ),
            "ExpressionAttributeNames": _LIFECYCLE_NAMES,
            "ExpressionAttributeValues": {
                ":deleting": _DELETING,
                ":staged": _STAGED,
                ":retired": _RETIRED,
                ":now": claimed_at,
            },
        }

        def after_cancellation(record: Mapping[str, Any]) -> bool | None:
            lifecycle = record.get("lifecycle")
            if lifecycle == _DELETING:
                # True when this very claim was applied but its response was lost.
                return record.get("claimed_at") == claimed_at
            delete_after = (record.get("delete_after") or {}).get("S")
            still_due = isinstance(delete_after, str) and delete_after <= claimed_at["S"]
            return None if lifecycle in (_STAGED, _RETIRED) and still_due else False

        return self._transact_unless_active(
            {"Update": claim},
            after_cancellation,
            series_id,
            observation_date,
            generation,
            deadline=deadline,
            clock=clock,
        )

    def iter_cleanup_records(
        self,
        *,
        series_id: str,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> Iterator[CleanupRecord | MalformedCleanupRecord]:
        """Yield every cleanup record of a series, following pagination.

        A record that cannot be interpreted is yielded as
        ``MalformedCleanupRecord`` so the caller can report it and go on.
        """

        validate_series_id(series_id)
        for item in self._query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": _series_key(series_id)},
                ":prefix": {"S": _CLEANUP_PREFIX},
            },
            deadline=deadline,
            clock=clock,
        ):
            try:
                yield _parse_cleanup_record(series_id, item)
            except DynamoDBDataError:
                sort_key = (item.get("sk") or {}).get("S")
                yield MalformedCleanupRecord(
                    series_id=series_id,
                    sort_key=sort_key if isinstance(sort_key, str) else None,
                )

    def sweep_generation(
        self,
        *,
        series_id: str,
        observation_date: str,
        generation: str,
        max_items: int | None = None,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> SweepResult:
        """Delete the items of a generation claimed for cleanup.

        Nothing is deleted unless, immediately beforehand, the cleanup record
        is ``deleting`` and the manifest does not reference the generation. The
        cleanup record itself is kept. Sweeping again is safe.

        Items are read one query page at a time and deleted in batches of at
        most 25. With ``max_items`` the sweep stops once that many deletes
        have been acknowledged. With ``deadline`` it stops before starting a
        batch or retry at or after that time. Either limit reports incomplete
        work. A failure after the first batch raises ``DynamoDBSweepError``
        with the number of deletes acknowledged so far.
        """

        self._check_generation(series_id, observation_date, generation)
        if max_items is not None and (
            isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0
        ):
            raise ValueError("max_items must be a non-negative integer")
        if deadline is not None and clock() >= deadline:
            return SweepResult(acknowledged=0, complete=False)
        if self._lifecycle(series_id, observation_date, generation) != _DELETING:
            raise DynamoDBConcurrencyError(
                "The generation is not claimed for cleanup; nothing was deleted"
            )
        if deadline is not None and clock() >= deadline:
            return SweepResult(acknowledged=0, complete=False)
        manifest = self._read_manifest(series_id, observation_date)
        if manifest is not None and manifest.generation == generation:
            raise DynamoDBDataError(
                "The manifest references a generation claimed for cleanup; nothing was deleted"
            )
        if deadline is not None and clock() >= deadline:
            return SweepResult(acknowledged=0, complete=False)

        acknowledged = 0
        batch: list[dict[str, Any]] = []
        try:
            for item in self._query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": _generation_key(series_id, observation_date, generation)},
                },
                ProjectionExpression="pk, sk",
                deadline=deadline,
                clock=clock,
            ):
                if deadline is not None and clock() >= deadline:
                    return SweepResult(acknowledged=acknowledged, complete=False)
                if max_items is not None and acknowledged + len(batch) >= max_items:
                    deleted, _ = self._write_delete_batch(
                        batch, deadline=deadline, clock=clock
                    )
                    acknowledged += deleted
                    return SweepResult(acknowledged=acknowledged, complete=False)
                batch.append({"DeleteRequest": {"Key": {"pk": item["pk"], "sk": item["sk"]}}})
                if len(batch) == _BATCH_WRITE_LIMIT:
                    deleted, complete = self._write_delete_batch(
                        batch, deadline=deadline, clock=clock
                    )
                    acknowledged += deleted
                    if not complete:
                        return SweepResult(acknowledged=acknowledged, complete=False)
                    batch = []
            if deadline is not None and clock() >= deadline:
                return SweepResult(acknowledged=acknowledged, complete=False)
            deleted, complete = self._write_delete_batch(
                batch, deadline=deadline, clock=clock
            )
            acknowledged += deleted
            if not complete:
                return SweepResult(acknowledged=acknowledged, complete=False)
        except DynamoDBSweepError as error:
            raise DynamoDBSweepError(
                str(error), acknowledged=acknowledged + error.acknowledged
            ) from None
        except DynamoDBStoreError as error:
            raise DynamoDBSweepError(str(error), acknowledged=acknowledged) from None
        return SweepResult(acknowledged=acknowledged, complete=True)

    def finalize_generation_cleanup(
        self,
        *,
        series_id: str,
        observation_date: str,
        generation: str,
        claimed_at: datetime,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> bool:
        """Remove the cleanup record of an empty, claimed generation.

        Returns ``False`` and removes nothing when the generation still has
        items, the record is no longer the claim identified by ``claimed_at``,
        or the manifest references the generation. Call this only once late
        writers can no longer add items; ``asof.dynamodb_cleanup`` waits for
        its stabilization period first.
        """

        self._check_generation(series_id, observation_date, generation)
        expected_claim = {"S": _timestamp(claimed_at)}
        remove = {
            "TableName": self._table_name,
            "Key": _cleanup_key(series_id, observation_date, generation),
            "ConditionExpression": "#lifecycle = :deleting AND claimed_at = :claimed_at",
            "ExpressionAttributeNames": _LIFECYCLE_NAMES,
            "ExpressionAttributeValues": {":deleting": _DELETING, ":claimed_at": expected_claim},
        }

        def still_empty() -> bool:
            if deadline is not None and clock() >= deadline:
                return False
            remaining = self._query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={
                    ":pk": {"S": _generation_key(series_id, observation_date, generation)},
                },
                ProjectionExpression="pk",
                Limit=1,
                deadline=deadline,
                clock=clock,
            )
            return next(iter(remaining), None) is None

        def after_cancellation(record: Mapping[str, Any]) -> bool | None:
            same_claim = (
                record.get("lifecycle") == _DELETING
                and record.get("claimed_at") == expected_claim
            )
            return None if same_claim and still_empty() else False

        if not still_empty():
            return False
        return self._transact_unless_active(
            {"Delete": remove},
            after_cancellation,
            series_id,
            observation_date,
            generation,
            deadline=deadline,
            clock=clock,
        )

    def is_ready(self) -> bool:
        """Return whether the table answers a read with the expected key schema."""

        if self._closed:
            return False
        try:
            self._call(
                "get_item",
                TableName=self._table_name,
                Key=_READINESS_KEY,
                ProjectionExpression="pk",
            )
        except DynamoDBStoreError:
            return False
        return True

    def close(self) -> None:
        """Release the client. A client supplied by the caller is left open."""

        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> DynamoDBRevisionStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise DynamoDBStoreError("The DynamoDB store is closed")

    def _ensure_writable(self) -> None:
        self._ensure_open()
        if self._read_only:
            raise DynamoDBStoreError("The DynamoDB store was opened read-only")

    def _check_generation(self, series_id: str, observation_date: str, generation: str) -> None:
        validate_series_id(series_id)
        validate_iso_date(observation_date, "observation_date")
        if not isinstance(generation, str) or not _GENERATION_ID.fullmatch(generation):
            raise ValueError("generation must be a 32-character lowercase hex identifier")
        self._ensure_writable()

    def _transact_unless_active(
        self,
        action: dict[str, Any],
        after_cancellation: Callable[[Mapping[str, Any]], bool | None],
        series_id: str,
        observation_date: str,
        generation: str,
        *,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> bool:
        """Run one write together with a check that the manifest points elsewhere.

        A cancelled transaction does not say why it was cancelled in a form
        worth trusting, so the cleanup record and the manifest are read again.
        ``after_cancellation`` receives the cleanup record (empty when it is
        gone) and returns the result to report, or ``None`` when the write's
        own condition still holds. In that case, and if the manifest still
        points elsewhere, nothing explains the cancellation: it is retried,
        and reported as a failure when the attempts run out.
        """

        not_active = {
            "TableName": self._table_name,
            "Key": _manifest_key(series_id, observation_date),
            "ConditionExpression": (
                "attribute_not_exists(active_generation) OR active_generation <> :generation"
            ),
            "ExpressionAttributeValues": {":generation": {"S": generation}},
        }
        for attempt in range(self._max_attempts):
            if deadline is not None and clock() >= deadline:
                return False
            try:
                self._call(
                    "transact_write_items",
                    TransactItems=[action, {"ConditionCheck": not_active}],
                )
                return True
            except DynamoDBRequestError as error:
                if error.code != _TRANSACTION_CANCELED:
                    raise
            if deadline is not None and clock() >= deadline:
                return False
            record = self._cleanup_record(series_id, observation_date, generation)
            result = after_cancellation(record)
            if result is not None:
                return result
            if deadline is not None and clock() >= deadline:
                return False
            manifest = self._read_manifest(series_id, observation_date)
            if manifest is not None and manifest.generation == generation:
                return False
            if attempt + 1 < self._max_attempts:
                if deadline is not None and clock() >= deadline:
                    return False
                self._backoff(attempt)
        raise DynamoDBRetryExhaustedError(
            f"DynamoDB kept cancelling a cleanup transaction after {self._max_attempts} "
            "attempts although its conditions still held"
        )

    def _call(self, operation: str, **parameters: Any) -> Any:
        self._ensure_open()
        try:
            return getattr(self._client, operation)(**parameters)
        except Exception as error:
            translated = _translate(operation, error)
            if translated is None:
                raise
            raise translated from None

    def _backoff(self, attempt: int) -> None:
        ceiling = min(self._backoff_cap, self._backoff_base * (2**attempt))
        delay = random.uniform(0, ceiling)
        if delay > 0:
            time.sleep(delay)

    def _query(
        self,
        *,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
        **parameters: Any,
    ) -> Iterator[Mapping[str, Any]]:
        """Yield every matching item, following pagination to the end."""

        request = {"TableName": self._table_name, "ConsistentRead": True, **parameters}
        while True:
            if deadline is not None and clock() >= deadline:
                return
            response = self._call("query", **request)
            yield from response.get("Items", ())
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return
            request["ExclusiveStartKey"] = last_key

    def _read_manifest(self, series_id: str, observation_date: str) -> _Manifest | None:
        response = self._call(
            "get_item",
            TableName=self._table_name,
            Key=_manifest_key(series_id, observation_date),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return _parse_manifest(item)

    def _generation_items(
        self,
        series_id: str,
        observation_date: str,
        generation: str,
    ) -> Iterator[Mapping[str, Any]]:
        return self._query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={
                ":pk": {"S": _generation_key(series_id, observation_date, generation)},
            },
            ScanIndexForward=True,
        )

    def _holds(self, manifest: _Manifest | None, replacement: _Replacement) -> bool:
        """Return whether a manifest's generation stores exactly this history.

        The stored items are compared, not just the manifest, so saving a
        history again repairs a generation that lost or changed items.
        """

        if manifest is None or manifest.vintage_count != len(replacement.vintages):
            return False
        expected = iter(replacement.vintages)
        for item in self._generation_items(
            replacement.series_id,
            replacement.observation_date,
            manifest.generation,
        ):
            wanted = next(expected, None)
            if wanted is None:
                return False
            try:
                stored = (_string(item, "sk"), Decimal(_number_text(item, "value")))
            except (DynamoDBDataError, ArithmeticError):
                return False
            if stored != (f"{_VINTAGE_PREFIX}{wanted[0]}", Decimal(wanted[1])):
                return False
        return next(expected, None) is None

    def _cleanup_record(
        self,
        series_id: str,
        observation_date: str,
        generation: str,
    ) -> Mapping[str, Any]:
        response = self._call(
            "get_item",
            TableName=self._table_name,
            Key=_cleanup_key(series_id, observation_date, generation),
            ConsistentRead=True,
        )
        return response.get("Item") or {}

    def _lifecycle(self, series_id: str, observation_date: str, generation: str) -> Any:
        return self._cleanup_record(series_id, observation_date, generation).get("lifecycle")

    def _replace_history(self, replacement: _Replacement) -> None:
        series_id = replacement.series_id
        observation_date = replacement.observation_date
        previous = self._read_manifest(series_id, observation_date)
        if self._holds(previous, replacement):
            return

        generation = uuid4().hex
        deadline = _utc_now() + GENERATION_GRACE_PERIOD
        # The cleanup record goes first so a crash while staging cannot leave
        # vintage items that nothing tracks.
        self._call(
            "put_item",
            TableName=self._table_name,
            Item={
                **_cleanup_key(series_id, observation_date, generation),
                "generation_key": {
                    "S": _generation_key(series_id, observation_date, generation)
                },
                "lifecycle": _STAGED,
                "delete_after": {"S": _timestamp(deadline)},
            },
        )
        self._stage(replacement, generation, deadline)

        for attempt in range(self._max_attempts):
            try:
                self._publish(replacement, generation, previous)
                return
            except DynamoDBRequestError as error:
                if error.code != _TRANSACTION_CANCELED:
                    raise
            current = self._read_manifest(series_id, observation_date)
            if current is not None and current.generation == generation:
                return
            if current != previous:
                if self._holds(current, replacement):
                    return
                raise DynamoDBConcurrencyError(
                    f"Another writer published {series_id} {observation_date} first; "
                    "the stored history was left unchanged"
                )
            if self._lifecycle(series_id, observation_date, generation) != _STAGED:
                raise DynamoDBStoreError(
                    "The staged generation was claimed for cleanup; nothing was published"
                )
            # Neither the pointer nor the claim moved, so the cancellation was transient.
            if attempt + 1 < self._max_attempts:
                self._backoff(attempt)
        raise DynamoDBRetryExhaustedError(
            f"DynamoDB kept cancelling publication after {self._max_attempts} attempts"
        )

    def _stage(self, replacement: _Replacement, generation: str, deadline: datetime) -> None:
        partition = _generation_key(
            replacement.series_id,
            replacement.observation_date,
            generation,
        )
        requests = [
            {
                "PutRequest": {
                    "Item": {
                        "pk": {"S": partition},
                        "sk": {"S": f"{_VINTAGE_PREFIX}{vintage_date}"},
                        "value": {"N": number},
                    }
                }
            }
            for vintage_date, number in replacement.vintages
        ]
        for start in range(0, len(requests), _BATCH_WRITE_LIMIT):
            self._write_batch(requests[start : start + _BATCH_WRITE_LIMIT], deadline=deadline)

    def _write_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        deadline: datetime | None = None,
    ) -> None:
        """Send up to 25 put or delete requests, resending unprocessed ones.

        With a ``deadline``, nothing is sent once it has passed. Cleanup may
        claim a staged generation from then on, and it must be able to assume
        that the writer has stopped adding items.
        """

        pending = requests
        for attempt in range(self._max_attempts):
            if not pending:
                return
            if deadline is not None and _utc_now() >= deadline:
                raise DynamoDBStoreError(
                    "Staging outlived the cleanup grace period; nothing was published"
                )
            response = self._call(
                "batch_write_item",
                RequestItems={self._table_name: pending},
            )
            unprocessed = response.get("UnprocessedItems") or {}
            pending = list(unprocessed.get(self._table_name) or ())
            if any(
                leftover
                for table, leftover in unprocessed.items()
                if table != self._table_name
            ):
                # Treating these as done would publish an incomplete
                # generation or leave items behind after a sweep.
                raise DynamoDBStoreError(
                    "DynamoDB reported unprocessed writes under an unexpected table name"
                )
            if pending and attempt + 1 < self._max_attempts:
                self._backoff(attempt)
        if pending:
            raise DynamoDBRetryExhaustedError(
                f"DynamoDB left batched writes unprocessed after {self._max_attempts} attempts",
                unprocessed=len(pending),
            )

    def _write_delete_batch(
        self,
        requests: list[dict[str, Any]],
        *,
        deadline: datetime | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> tuple[int, bool]:
        """Delete one batch and preserve acknowledged progress on every failure."""

        pending = requests
        acknowledged = 0
        for attempt in range(self._max_attempts):
            if not pending:
                return acknowledged, True
            if deadline is not None and clock() >= deadline:
                return acknowledged, False
            before = len(pending)
            try:
                response = self._call(
                    "batch_write_item",
                    RequestItems={self._table_name: pending},
                )
            except DynamoDBStoreError as error:
                raise DynamoDBSweepError(str(error), acknowledged=acknowledged) from None
            unprocessed = response.get("UnprocessedItems") or {}
            pending = list(unprocessed.get(self._table_name) or ())
            acknowledged += before - len(pending)
            if any(
                leftover
                for table, leftover in unprocessed.items()
                if table != self._table_name
            ):
                raise DynamoDBSweepError(
                    "DynamoDB reported unprocessed writes under an unexpected table name",
                    acknowledged=acknowledged,
                )
            if pending and attempt + 1 < self._max_attempts:
                self._backoff(attempt)
        if pending:
            raise DynamoDBSweepError(
                f"DynamoDB left batched writes unprocessed after {self._max_attempts} attempts",
                acknowledged=acknowledged,
            )
        return acknowledged, True

    def _publish(
        self,
        replacement: _Replacement,
        generation: str,
        previous: _Manifest | None,
    ) -> None:
        series_id = replacement.series_id
        observation_date = replacement.observation_date
        published_at = _utc_now()
        manifest: dict[str, Any] = {
            "TableName": self._table_name,
            "Item": {
                **_manifest_key(series_id, observation_date),
                "active_generation": {"S": generation},
                "vintage_count": {"N": str(len(replacement.vintages))},
                "updated_at": {"S": _timestamp(published_at)},
            },
            "ConditionExpression": "attribute_not_exists(active_generation)",
        }
        # Fails once a cleanup claim has moved the record out of "staged".
        activate = {
            "TableName": self._table_name,
            "Key": _cleanup_key(series_id, observation_date, generation),
            "UpdateExpression": "SET #lifecycle = :active REMOVE delete_after",
            "ConditionExpression": "#lifecycle = :staged",
            "ExpressionAttributeNames": _LIFECYCLE_NAMES,
            "ExpressionAttributeValues": {":active": _ACTIVE, ":staged": _STAGED},
        }
        transaction = [{"Put": manifest}, {"Update": activate}]
        if previous is not None:
            manifest["ConditionExpression"] = "active_generation = :expected"
            manifest["ExpressionAttributeValues"] = {":expected": {"S": previous.generation}}
            retire = {
                "TableName": self._table_name,
                "Key": _cleanup_key(series_id, observation_date, previous.generation),
                "UpdateExpression": (
                    "SET #lifecycle = :retired, delete_after = :delete_after, "
                    "generation_key = :key"
                ),
                "ExpressionAttributeNames": _LIFECYCLE_NAMES,
                "ExpressionAttributeValues": {
                    ":retired": _RETIRED,
                    ":delete_after": {
                        "S": _timestamp(published_at + GENERATION_GRACE_PERIOD)
                    },
                    ":key": {
                        "S": _generation_key(series_id, observation_date, previous.generation)
                    },
                },
            }
            transaction.append({"Update": retire})
        self._call("transact_write_items", TransactItems=transaction)
