"""Delete DynamoDB generations that are no longer needed.

Every replaced history leaves its previous generation in the table, and every
failed save leaves a staged one. ``clean_up_generations`` removes them for the
series it is given. It does not scan the table, so it never finds series that
were not named.

One call makes one pass and never waits. For each cleanup record of a series:

1. ``active`` records, and ``staged`` or ``retired`` records whose
   ``delete_after`` time has not arrived, are skipped.
2. A due ``staged`` or ``retired`` record is claimed, which moves it to
   ``deleting`` and stores the claim time. A ``deleting`` record left by an
   earlier pass is resumed.
3. The generation's items are deleted. The cleanup record is kept.
4. The cleanup record is removed only when the stabilization period has passed
   since the claim, this pass found no items, and the manifest does not
   reference the generation. Otherwise the record is deferred to a later pass.

Step 4 exists because a writer whose staged generation was claimed can still
have a batch of items on its way. A writer stops staging once the generation's
``delete_after`` time passes, and a claim requires that time to have passed, so
such items come only from a request already in flight or from a writer whose
clock runs behind. The stabilization period must be longer than the two
together. Until it has passed, the record stays so a later pass sweeps again.

A pass can be interrupted at any point and run again. Each step is safe to
repeat, and a ``deleting`` generation can never be published.

A pass can also be given budgets: how many generations it may claim or resume
deleting, how many deletes it may have acknowledged, and how long it may run. When a budget
runs out the pass stops, says so in its summary, and leaves the rest for the
next pass. Nothing is stored between passes. A pass starts again from the
first named series, so budgets must be large enough for cleanup to keep up
with the generations that syncs retire.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .dynamodb import (
    CleanupRecord,
    DynamoDBConcurrencyError,
    DynamoDBDataError,
    DynamoDBRevisionStore,
    DynamoDBStoreError,
    DynamoDBSweepError,
    MalformedCleanupRecord,
)
from .models import validate_series_id

# How long one batch write can stay in flight. With the store's default client
# settings a call makes at most six attempts of up to twenty seconds each
# (connect plus read timeout), with waits of at most twenty seconds in between.
IN_FLIGHT_WRITE_ALLOWANCE = timedelta(minutes=5)

# A conservative allowance for disagreement between writer and cleanup clocks.
# This deliberately exceeds the smaller tolerance commonly observed for
# signed AWS requests, because cleanup safety should not depend on that detail.
CLOCK_SKEW_ALLOWANCE = timedelta(minutes=30)

# A writer sends nothing after its generation's delete_after time, and a claim
# needs that time to have passed. Items can therefore still arrive after a
# claim only from a request already in flight, or because the writer's clock
# is behind. No shorter stabilization period is accepted.
MINIMUM_STABILIZATION_PERIOD = IN_FLIGHT_WRITE_ALLOWANCE + CLOCK_SKEW_ALLOWANCE

# Default time between a claim and the removal of its cleanup record.
STABILIZATION_PERIOD = timedelta(minutes=45)

# Conservative defaults for a future Lambda invocation. Callers may choose
# smaller positive budgets, or explicitly pass ``None`` for an unbounded local
# maintenance run.
DEFAULT_MAX_RECORDS = 25
DEFAULT_MAX_DELETES = 500
DEFAULT_TIME_BUDGET = timedelta(minutes=8)


@dataclass(frozen=True)
class CleanupFailure:
    """One problem found during a cleanup pass.

    ``record`` is the cleanup record's sort key, or ``None`` when the failure
    was not tied to a single record. ``acknowledged_deletes`` counts the
    delete requests DynamoDB accepted for that record before the failure.
    """

    series_id: str
    record: str | None
    reason: str
    acknowledged_deletes: int = 0


@dataclass(frozen=True)
class CleanupSummary:
    """What one cleanup pass did.

    Every examined record ends up counted once as skipped, deferred,
    completed, or failed. ``claimed`` and ``swept`` describe the work done
    along the way; ``swept`` counts generations whose partition was read to
    the end.

    ``acknowledged_deletes`` counts delete requests that DynamoDB accepted:
    sent in a batch and not returned as unprocessed. It includes deletes made
    before a failure or before a budget ran out. It is not a count of items
    known to be gone.

    ``stopped_early`` means a failure ended the pass. ``budget_exhausted``
    means a record, delete, or time budget ended it. Either way some records
    were not examined.
    """

    examined: int
    skipped: int
    claimed: int
    swept: int
    acknowledged_deletes: int
    deferred: int
    completed: int
    failures: tuple[CleanupFailure, ...]
    stopped_early: bool
    budget_exhausted: bool

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def succeeded(self) -> bool:
        """Whether every requested series was examined without a failure."""

        return not self.failures and not self.stopped_early and not self.budget_exhausted


class _Pass:
    def __init__(
        self,
        store: DynamoDBRevisionStore,
        stabilization_period: timedelta,
        clock: Callable[[], datetime],
        max_records: int | None,
        max_deletes: int | None,
        time_budget: timedelta | None,
    ) -> None:
        self.store = store
        self.stabilization_period = stabilization_period
        self.clock = clock
        self.max_records = max_records
        self.max_deletes = max_deletes
        self.stop_at = None if time_budget is None else clock() + time_budget
        self.counts: Counter[str] = Counter()
        self.failures: list[CleanupFailure] = []
        self.stopped_early = False
        self.budget_exhausted = False

    def clean_series(self, series_id: str) -> bool:
        """Process one series. Return ``False`` when the pass must stop."""

        current: str | None = None
        try:
            for record in self.store.iter_cleanup_records(
                series_id=series_id,
                deadline=self.stop_at,
                clock=self.clock,
            ):
                if self.stop_at is not None and self.clock() >= self.stop_at:
                    self.budget_exhausted = True
                    return False
                self.counts["examined"] += 1
                current = record.sort_key
                if isinstance(record, MalformedCleanupRecord):
                    self.fail(series_id, current, "The cleanup record is malformed")
                else:
                    try:
                        self.counts[self.clean_generation(record)] += 1
                    except DynamoDBConcurrencyError:
                        self.counts["deferred"] += 1
                    except DynamoDBDataError as error:
                        self.fail(series_id, current, str(error))
                    if self.budget_exhausted:
                        return False
                current = None
            if self.stop_at is not None and self.clock() >= self.stop_at:
                self.budget_exhausted = True
                return False
        except DynamoDBStoreError as error:
            # The table or the retry budget is the problem, not one record,
            # so later records would fail the same way.
            self.fail(series_id, current, str(error), getattr(error, "acknowledged", 0))
            self.stopped_early = True
            return False
        return True

    def clean_generation(self, record: CleanupRecord) -> str:
        """Advance one record as far as this pass may; return its outcome."""

        generation = {
            "series_id": record.series_id,
            "observation_date": record.observation_date,
            "generation": record.generation,
        }
        claimed_at = record.claimed_at
        resumed = record.lifecycle == "deleting"
        out_of_records = (
            self.max_records is not None and self.counts["worked"] >= self.max_records
        )
        if not resumed:
            now = self.clock()
            if record.delete_after is None or record.delete_after > now:
                return "skipped"
            if out_of_records:
                self.budget_exhausted = True
                return "deferred"
            claimed = self.store.claim_generation_for_cleanup(
                **generation,
                now=now,
                deadline=self.stop_at,
                clock=self.clock,
            )
            if claimed:
                self.counts["claimed"] += 1
                self.counts["worked"] += 1
                claimed_at = now
            if self.stop_at is not None and self.clock() >= self.stop_at:
                self.budget_exhausted = True
                return "deferred"
            if not claimed:
                return "deferred"
        if claimed_at is None:
            raise DynamoDBDataError("The cleanup record has no claim time")

        deletes_left = None
        if out_of_records:
            # Still look, without deleting: an empty generation may be finalized.
            deletes_left = 0
        elif self.max_deletes is not None:
            deletes_left = self.max_deletes - self.counts["acknowledged_deletes"]
        try:
            sweep = self.store.sweep_generation(
                **generation,
                max_items=deletes_left,
                deadline=self.stop_at,
                clock=self.clock,
            )
        except DynamoDBSweepError as error:
            self.counts["acknowledged_deletes"] += error.acknowledged
            raise
        self.counts["acknowledged_deletes"] += sweep.acknowledged
        if resumed and sweep.acknowledged:
            self.counts["worked"] += 1
        if not sweep.complete:
            self.budget_exhausted = True
            return "deferred"
        self.counts["swept"] += 1
        if sweep.acknowledged or self.clock() < claimed_at + self.stabilization_period:
            return "deferred"
        if self.stop_at is not None and self.clock() >= self.stop_at:
            self.budget_exhausted = True
            return "deferred"
        finalized = self.store.finalize_generation_cleanup(
            **generation,
            claimed_at=claimed_at,
            deadline=self.stop_at,
            clock=self.clock,
        )
        if self.stop_at is not None and self.clock() >= self.stop_at:
            self.budget_exhausted = True
        if finalized:
            return "completed"
        return "deferred"

    def fail(
        self,
        series_id: str,
        record: str | None,
        reason: str,
        acknowledged_deletes: int = 0,
    ) -> None:
        self.failures.append(
            CleanupFailure(
                series_id=series_id,
                record=record,
                reason=reason,
                acknowledged_deletes=acknowledged_deletes,
            )
        )


def _positive_count(value: int | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be a positive integer")


def clean_up_generations(
    store: DynamoDBRevisionStore,
    series_ids: Iterable[str],
    *,
    stabilization_period: timedelta = STABILIZATION_PERIOD,
    max_records: int | None = DEFAULT_MAX_RECORDS,
    max_deletes: int | None = DEFAULT_MAX_DELETES,
    time_budget: timedelta | None = DEFAULT_TIME_BUDGET,
    clock: Callable[[], datetime] | None = None,
) -> CleanupSummary:
    """Make one cleanup pass over the named series and report what happened.

    ``stabilization_period`` may not be shorter than the store's estimated
    maximum in-flight SDK request plus ``CLOCK_SKEW_ALLOWANCE``. Custom client
    timeout and retry settings therefore raise the required minimum.

    The budgets have conservative finite defaults. Explicitly pass ``None``
    only for an unbounded local maintenance run. ``max_records`` limits how many generations the
    pass claims or resumes deleting; skipped records, and claimed generations
    that are already empty, do not count. ``max_deletes`` limits the delete
    requests it has acknowledged. ``time_budget`` is checked before each
    record, delete batch and delete retry. An in-flight SDK request may finish
    after the deadline, but no new request starts after it. A pass that runs
    out of budget sets ``budget_exhausted``.

    A malformed record, or one that contradicts the manifest, is reported in
    ``failures`` and left untouched; the pass goes on to the next record. A
    failed request or exhausted retries is reported too, but stops the pass,
    and ``stopped_early`` is set. Storage errors are never raised from here:
    check ``succeeded``. Invalid arguments raise before any request is made.
    """

    required_stabilization = store.in_flight_write_allowance + CLOCK_SKEW_ALLOWANCE
    if stabilization_period < required_stabilization:
        raise ValueError(
            f"stabilization_period must be at least {required_stabilization} "
            "for this store's client configuration"
        )
    _positive_count(max_records, "max_records")
    _positive_count(max_deletes, "max_deletes")
    if time_budget is not None and time_budget <= timedelta(0):
        raise ValueError("time_budget must be positive")
    series = tuple(dict.fromkeys(validate_series_id(series_id) for series_id in series_ids))
    cleanup = _Pass(
        store,
        stabilization_period,
        clock or (lambda: datetime.now(timezone.utc)),
        max_records,
        max_deletes,
        time_budget,
    )
    for series_id in series:
        if not cleanup.clean_series(series_id):
            break
    counts = cleanup.counts
    return CleanupSummary(
        examined=counts["examined"],
        skipped=counts["skipped"],
        claimed=counts["claimed"],
        swept=counts["swept"],
        acknowledged_deletes=counts["acknowledged_deletes"],
        deferred=counts["deferred"],
        completed=counts["completed"],
        failures=tuple(cleanup.failures),
        stopped_early=cleanup.stopped_early,
        budget_exhausted=cleanup.budget_exhausted,
    )
