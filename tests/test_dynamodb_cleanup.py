import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import dynamodb_support
from dynamodb_support import TABLE_NAME, InterceptingClient, client_error
from asof import (
    CleanupSummary,
    DynamoDBConcurrencyError,
    DynamoDBDataError,
    DynamoDBRequestError,
    DynamoDBRevisionStore,
    DynamoDBRetryExhaustedError,
    DynamoDBStoreError,
    DynamoDBSweepError,
    RevisionHistory,
    VintageValue,
    clean_up_generations,
    dynamodb,
)
from asof.dynamodb import (
    GENERATION_GRACE_PERIOD,
    CleanupRecord,
    MalformedCleanupRecord,
    SweepResult,
)
from asof.dynamodb_cleanup import (
    CLOCK_SKEW_ALLOWANCE,
    DEFAULT_MAX_DELETES,
    DEFAULT_MAX_RECORDS,
    DEFAULT_TIME_BUDGET,
    IN_FLIGHT_WRITE_ALLOWANCE,
    MINIMUM_STABILIZATION_PERIOD,
    STABILIZATION_PERIOD,
)

START = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FIRST_VINTAGE = date(2008, 9, 5)
JUST_UNDER_GRACE = GENERATION_GRACE_PERIOD - timedelta(seconds=1)
JUST_UNDER_STABILIZATION = STABILIZATION_PERIOD - timedelta(seconds=1)


def history(count=2, observation_date="2008-08-01", series_id="PAYEMS", base=1000.0):
    return RevisionHistory(
        series_id=series_id,
        observation_date=observation_date,
        revisions=tuple(
            VintageValue((FIRST_VINTAGE + timedelta(days=day)).isoformat(), base + day)
            for day in range(count)
        ),
    )


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, period):
        self.now += period


def once(action):
    """Wrap a hook so it only acts on the first call."""

    pending = [action]

    def hook(parameters):
        if pending:
            pending.pop()(parameters)

    return hook


def fail_with(code, operation):
    def action(parameters):
        raise client_error(code, operation, "arn:aws:dynamodb:us-east-2:123456789012:table/private")

    return action


@unittest.skipUnless(dynamodb_support.AVAILABLE, dynamodb_support.SKIP_REASON)
class CleanupTestCase(unittest.TestCase):
    def setUp(self):
        self.local = dynamodb_support.LocalDynamoDB().start()
        self.addCleanup(self.local.stop)
        self.client = InterceptingClient(self.local.client)
        self.store = self.make_store()
        self.clock = Clock()
        # Writers and cleanup share one controllable clock.
        patcher = patch.object(dynamodb, "_utc_now", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_store(self, **options):
        options.setdefault("backoff_base_seconds", 0)
        options.setdefault("backoff_cap_seconds", 0)
        return DynamoDBRevisionStore(TABLE_NAME, client=self.client, **options)

    def clean(self, *series_ids, store=None, **options):
        summary = clean_up_generations(
            store or self.store,
            series_ids or ("PAYEMS",),
            clock=self.clock,
            **options,
        )
        self.assertIsInstance(summary, CleanupSummary)
        if not summary.stopped_early:
            self.assertEqual(
                summary.examined,
                summary.skipped + summary.deferred + summary.completed + summary.failed,
            )
        return summary

    def active_key(self, observation_date="2008-08-01", series_id="PAYEMS"):
        (manifest,) = [
            item
            for item in self.local.manifests()
            if item["pk"]["S"] == f"SERIES#{series_id}"
            and item["sk"]["S"] == f"OBS#{observation_date}"
        ]
        generation = manifest["active_generation"]["S"]
        return f"SERIES#{series_id}#OBS#{observation_date}#GEN#{generation}"

    def record(self, generation_key):
        found = [
            item
            for item in self.local.cleanup_records()
            if item.get("generation_key", {}).get("S") == generation_key
        ]
        return found[0] if found else None

    def lifecycle(self, generation_key):
        return self.record(generation_key)["lifecycle"]["S"]

    def replace(self, old_count=2, new_count=3, **where):
        """Publish one generation, replace it, and return the retired one's key."""

        self.store.save_history(history(old_count, **where))
        retired_key = self.active_key(**where)
        self.store.save_history(history(new_count, base=2000.0, **where))
        return retired_key

    def abandon(self, count=2, at_batch=None):
        """Fail a save so its staged generation is left behind; return its key."""

        if at_batch is None:
            self.client.before(
                "transact_write_items",
                once(fail_with("InternalServerError", "TransactWriteItems")),
            )
        else:
            seen = []

            def fail(parameters):
                seen.append(1)
                if len(seen) == at_batch:
                    fail_with("InternalServerError", "BatchWriteItem")(parameters)

            self.client.before("batch_write_item", fail)
        with self.assertRaises(DynamoDBRequestError):
            self.store.save_history(history(count))
        (record,) = self.local.cleanup_records()
        self.assertEqual(record["lifecycle"], {"S": "staged"})
        return record["generation_key"]["S"]

    def generation_arguments(self, generation_key):
        _, series_id, _, observation_date, _, generation = generation_key.split("#")
        return {
            "series_id": series_id,
            "observation_date": observation_date,
            "generation": generation,
        }

    def put_late_items(self, generation_key, count=3):
        for day in range(count):
            self.local.client.put_item(
                TableName=TABLE_NAME,
                Item={
                    "pk": {"S": generation_key},
                    "sk": {"S": f"VINTAGE#2031-01-{day + 1:02d}"},
                    "value": {"N": "1"},
                },
            )

    def point_manifest_at(self, generation_key):
        arguments = self.generation_arguments(generation_key)
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={
                "pk": {"S": f"SERIES#{arguments['series_id']}"},
                "sk": {"S": f"OBS#{arguments['observation_date']}"},
                "active_generation": {"S": arguments["generation"]},
                "vintage_count": {"N": "2"},
            },
        )


class LifecycleTests(CleanupTestCase):
    def test_replaced_generation_is_removed_over_two_passes(self):
        first = history(40)
        second = history(60, base=2000.0)
        self.store.save_history(first)
        first_key = self.active_key()
        self.store.save_history(second)
        second_key = self.active_key()

        self.clock.advance(GENERATION_GRACE_PERIOD)
        claimed = self.clean()

        self.assertEqual(
            (claimed.examined, claimed.skipped, claimed.claimed, claimed.swept),
            (2, 1, 1, 1),
        )
        self.assertEqual(claimed.acknowledged_deletes, 40)
        self.assertEqual((claimed.deferred, claimed.completed), (1, 0))
        self.assertTrue(claimed.succeeded)
        self.assertEqual(self.local.vintages(first_key), [])
        self.assertEqual(self.lifecycle(first_key), "deleting")
        self.assertEqual(self.record(first_key)["claimed_at"], {"S": "2026-01-01T12:30:00Z"})

        self.clock.advance(STABILIZATION_PERIOD)
        finished = self.clean()

        self.assertEqual(
            (finished.examined, finished.skipped, finished.claimed, finished.swept),
            (2, 1, 0, 1),
        )
        self.assertEqual(
            (finished.acknowledged_deletes, finished.deferred, finished.completed),
            (0, 0, 1),
        )
        self.assertTrue(finished.succeeded)
        self.assertIsNone(self.record(first_key))
        self.assertEqual(self.local.vintages(first_key), [])

        self.assertEqual(self.active_key(), second_key)
        self.assertEqual(self.lifecycle(second_key), "active")
        self.assertEqual(len(self.local.vintages(second_key)), 60)
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            second,
        )
        lookup = self.store.lookup_value_as_of(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            as_of_date="9999-12-31",
        )
        self.assertEqual(lookup.revision, second.current)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ("2008-08-01",))
        self.assertEqual(len(self.local.items()), 62)

    def test_generation_abandoned_by_a_failed_publication_is_removed(self):
        abandoned_key = self.abandon(count=30)
        self.assertEqual(len(self.local.vintages(abandoned_key)), 30)

        self.clock.advance(GENERATION_GRACE_PERIOD)
        claimed = self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        finished = self.clean()

        self.assertEqual((claimed.claimed, claimed.deferred), (1, 1))
        self.assertEqual(claimed.acknowledged_deletes, 30)
        self.assertEqual(finished.completed, 1)
        self.assertEqual(self.local.items(), [])

        self.store.save_history(history(30))
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(30),
        )

    def test_generation_abandoned_while_staging_is_removed(self):
        abandoned_key = self.abandon(count=60, at_batch=2)
        self.assertEqual(len(self.local.vintages(abandoned_key)), 25)

        self.clock.advance(GENERATION_GRACE_PERIOD)
        claimed = self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.clean()

        self.assertEqual(claimed.acknowledged_deletes, 25)
        self.assertEqual(self.local.items(), [])

    def test_generation_without_any_items_is_removed(self):
        abandoned_key = self.abandon(at_batch=1)
        self.assertEqual(self.local.vintages(abandoned_key), [])

        self.clock.advance(GENERATION_GRACE_PERIOD)
        claimed = self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        finished = self.clean()

        self.assertEqual((claimed.claimed, claimed.swept, claimed.acknowledged_deletes), (1, 1, 0))
        self.assertEqual(claimed.deferred, 1)
        self.assertEqual(finished.completed, 1)
        self.assertEqual(self.local.items(), [])

    def test_repeated_cleanup_changes_nothing_more(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.clean()
        settled = self.local.items()

        for _ in range(3):
            self.clock.advance(timedelta(days=1))
            summary = self.clean()

            self.assertEqual((summary.examined, summary.skipped), (1, 1))
            self.assertEqual(
                (summary.claimed, summary.swept, summary.deferred, summary.completed),
                (0, 0, 0, 0),
            )
            self.assertEqual(self.local.items(), settled)

    def test_cleanup_uses_no_scan_and_no_single_item_deletes(self):
        self.replace(old_count=30)
        self.client.calls.clear()

        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.clean()

        self.assertEqual(
            set(self.client.operations()),
            {"query", "get_item", "transact_write_items", "batch_write_item"},
        )
        for parameters in self.client.parameters("query") + self.client.parameters("get_item"):
            self.assertIs(parameters["ConsistentRead"], True)


class DiscoveryTests(CleanupTestCase):
    def test_series_without_cleanup_records(self):
        summary = self.clean("PAYEMS", "UNRATE")

        self.assertEqual(
            summary,
            CleanupSummary(
                examined=0,
                skipped=0,
                claimed=0,
                swept=0,
                acknowledged_deletes=0,
                deferred=0,
                completed=0,
                failures=(),
                stopped_early=False,
                budget_exhausted=False,
            ),
        )
        self.assertTrue(summary.succeeded)
        self.assertEqual(self.client.operations(), ["query", "query"])

    def test_only_named_series_are_cleaned(self):
        payems_key = self.replace()
        unrate_key = self.replace(series_id="UNRATE")
        gdp_key = self.replace(series_id="GDP")
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean("PAYEMS", "UNRATE")

        self.assertEqual((summary.examined, summary.claimed), (4, 2))
        self.assertEqual(self.lifecycle(payems_key), "deleting")
        self.assertEqual(self.lifecycle(unrate_key), "deleting")
        self.assertEqual(self.lifecycle(gdp_key), "retired")
        self.assertEqual(len(self.local.vintages(gdp_key)), 2)

    def test_similar_series_ids_are_not_confused(self):
        other_key = self.replace(series_id="PAYEMS.X")
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean("PAYEMS")

        self.assertEqual(summary.examined, 0)
        self.assertEqual(self.lifecycle(other_key), "retired")

    def test_repeated_series_ids_are_cleaned_once(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean("PAYEMS", "PAYEMS")

        self.assertEqual((summary.examined, summary.claimed), (2, 1))

    def test_series_ids_may_come_from_a_generator(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean(*(name for name in ("PAYEMS",)))

        self.assertEqual(summary.claimed, 1)

    def test_invalid_series_id_stops_before_any_request(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()

        for invalid in ("PAYEMS#OBS", "", "PAY EMS"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "series_id"):
                    self.clean("PAYEMS", invalid)

        self.assertEqual(self.client.calls, [])

    def test_invalid_stabilization_period_is_rejected(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()
        for period in (
            timedelta(0),
            timedelta(seconds=-1),
            timedelta(seconds=1),
            MINIMUM_STABILIZATION_PERIOD - timedelta(seconds=1),
        ):
            with self.subTest(period=period):
                with self.assertRaisesRegex(ValueError, "stabilization_period must be at least"):
                    self.clean(stabilization_period=period)

        self.assertEqual(self.client.calls, [])

    def test_invalid_budgets_are_rejected(self):
        for options in (
            {"max_records": 0},
            {"max_records": -1},
            {"max_records": 1.5},
            {"max_records": True},
            {"max_deletes": 0},
            {"max_deletes": "10"},
            {"time_budget": timedelta(0)},
            {"time_budget": timedelta(seconds=-5)},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.clean(**options)

        self.assertEqual(self.client.calls, [])

    def test_cleanup_records_are_found_across_query_pages(self):
        dates = [f"2008-{month:02d}-01" for month in range(1, 8)]
        retired = [self.replace(observation_date=day) for day in dates]
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.page_limit = 3
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual((summary.examined, summary.skipped, summary.claimed), (14, 7, 7))
        for generation_key in retired:
            self.assertEqual(self.lifecycle(generation_key), "deleting")
        discovery = [
            parameters
            for parameters in self.client.parameters("query")
            if ":prefix" in parameters["ExpressionAttributeValues"]
        ]
        self.assertGreaterEqual(len(discovery), 5)
        self.assertTrue(all("ExclusiveStartKey" in query for query in discovery[1:]))

    def test_records_removed_during_a_paged_pass_do_not_hide_later_ones(self):
        dates = [f"2008-{month:02d}-01" for month in range(1, 8)]
        retired = [self.replace(observation_date=day) for day in dates]
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.page_limit = 2

        summary = self.clean()

        self.assertEqual((summary.examined, summary.completed), (14, 7))
        for generation_key in retired:
            self.assertIsNone(self.record(generation_key))

    def test_iter_cleanup_records_describes_each_state(self):
        retired_key = self.replace()
        active_key = self.active_key()

        records = {
            record.generation: record
            for record in self.store.iter_cleanup_records(series_id="PAYEMS")
        }

        retired = records[retired_key.rsplit("#", 1)[1]]
        active = records[active_key.rsplit("#", 1)[1]]
        self.assertIsInstance(retired, CleanupRecord)
        self.assertEqual(
            (retired.lifecycle, retired.delete_after, retired.claimed_at),
            ("retired", START + GENERATION_GRACE_PERIOD, None),
        )
        self.assertEqual((active.lifecycle, active.delete_after), ("active", None))
        self.assertEqual(retired.sort_key, self.record(retired_key)["sk"]["S"])


class EligibilityTests(CleanupTestCase):
    def test_active_generation_is_skipped_without_any_write(self):
        self.store.save_history(history())
        active_key = self.active_key()
        before = self.local.items()
        self.clock.advance(timedelta(days=365))
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual((summary.examined, summary.skipped), (1, 1))
        self.assertEqual(self.client.operations(), ["query"])
        self.assertEqual(self.local.items(), before)
        self.assertEqual(self.lifecycle(active_key), "active")

    def test_retired_generation_is_skipped_until_its_deadline(self):
        retired_key = self.replace()
        before = self.local.items()
        self.clock.advance(JUST_UNDER_GRACE)
        self.client.calls.clear()

        early = self.clean()

        self.assertEqual((early.examined, early.skipped, early.claimed), (2, 2, 0))
        self.assertEqual(self.client.operations(), ["query"])
        self.assertEqual(self.local.items(), before)

        self.clock.advance(timedelta(seconds=1))
        due = self.clean()

        self.assertEqual((due.claimed, due.acknowledged_deletes), (1, 2))
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_abandoned_staged_generation_is_skipped_until_its_deadline(self):
        abandoned_key = self.abandon()
        self.clock.advance(JUST_UNDER_GRACE)

        early = self.clean()

        self.assertEqual((early.skipped, early.claimed), (1, 0))
        self.assertEqual(self.lifecycle(abandoned_key), "staged")
        self.assertEqual(len(self.local.vintages(abandoned_key)), 2)

        self.clock.advance(timedelta(seconds=1))
        due = self.clean()

        self.assertEqual((due.claimed, due.acknowledged_deletes), (1, 2))
        self.assertEqual(self.lifecycle(abandoned_key), "deleting")

    def test_generation_claimed_earlier_is_resumed(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.assertTrue(
            self.store.claim_generation_for_cleanup(
                **self.generation_arguments(retired_key),
                now=self.clock(),
            )
        )
        self.assertEqual(len(self.local.vintages(retired_key)), 2)

        summary = self.clean()

        self.assertEqual((summary.claimed, summary.swept, summary.acknowledged_deletes), (0, 1, 2))
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_due_record_the_manifest_references_is_not_claimed(self):
        # The record says "retired", but the manifest still points at it.
        retired_key = self.replace()
        self.point_manifest_at(retired_key)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual((summary.claimed, summary.swept, summary.acknowledged_deletes), (0, 0, 0))
        self.assertGreaterEqual(summary.deferred, 1)
        self.assertEqual(self.lifecycle(retired_key), "retired")
        self.assertEqual(len(self.local.vintages(retired_key)), 2)
        self.assertNotIn("batch_write_item", self.client.operations())

    def test_generation_published_just_before_the_claim_is_left_alone(self):
        # Cleanup lists a due staged record, then its writer publishes it.
        staged_key = self.abandon()
        arguments = self.generation_arguments(staged_key)
        cleanup_key = {"pk": {"S": "SERIES#PAYEMS"}, "sk": self.record(staged_key)["sk"]}

        def publish(parameters):
            self.local.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": {
                                "pk": {"S": "SERIES#PAYEMS"},
                                "sk": {"S": "OBS#2008-08-01"},
                                "active_generation": {"S": arguments["generation"]},
                                "vintage_count": {"N": "2"},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": TABLE_NAME,
                            "Key": cleanup_key,
                            "UpdateExpression": "SET #lifecycle = :active REMOVE delete_after",
                            "ConditionExpression": "#lifecycle = :staged",
                            "ExpressionAttributeNames": {"#lifecycle": "lifecycle"},
                            "ExpressionAttributeValues": {
                                ":active": {"S": "active"},
                                ":staged": {"S": "staged"},
                            },
                        }
                    },
                ]
            )

        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before("transact_write_items", once(publish))

        summary = self.clean()

        self.assertEqual((summary.claimed, summary.deferred), (0, 1))
        self.assertEqual(summary.acknowledged_deletes, 0)
        self.assertEqual(self.lifecycle(staged_key), "active")
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(),
        )


class DeletionTests(CleanupTestCase):
    def batch_sizes(self):
        return [
            len(parameters["RequestItems"][TABLE_NAME])
            for parameters in self.client.parameters("batch_write_item")
        ]

    def test_large_generation_is_deleted_in_batches_of_twenty_five(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual(summary.acknowledged_deletes, 60)
        self.assertEqual(self.batch_sizes(), [25, 25, 10])
        for parameters in self.client.parameters("batch_write_item"):
            for request in parameters["RequestItems"][TABLE_NAME]:
                self.assertEqual(set(request), {"DeleteRequest"})
                self.assertEqual(request["DeleteRequest"]["Key"]["pk"], {"S": retired_key})
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_generation_is_listed_across_query_pages_before_deletion(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.page_limit = 7

        summary = self.clean()

        self.assertEqual(summary.acknowledged_deletes, 60)
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_unprocessed_deletes_are_sent_again(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.process_at_most = 10
        self.client.calls.clear()

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual(summary.acknowledged_deletes, 60)
        self.assertEqual(self.batch_sizes(), [25, 15, 5, 25, 15, 5, 10])
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_deletes_that_stay_unprocessed_end_the_pass_after_bounded_attempts(self):
        retired_key = self.replace(old_count=30)
        other_key = self.replace(series_id="UNRATE")
        store = self.make_store(max_attempts=3)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.process_at_most = 0
        self.client.calls.clear()

        summary = self.clean("PAYEMS", "UNRATE", store=store)

        self.assertEqual(self.batch_sizes(), [25, 25, 25])
        self.assertFalse(summary.succeeded)
        self.assertTrue(summary.stopped_early)
        self.assertEqual((summary.claimed, summary.swept, summary.completed), (1, 0, 0))
        (failure,) = summary.failures
        self.assertEqual(failure.series_id, "PAYEMS")
        self.assertEqual(failure.record, self.record(retired_key)["sk"]["S"])
        self.assertIn("unprocessed after 3 attempts", failure.reason)
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertEqual(len(self.local.vintages(retired_key)), 30)
        self.assertEqual(self.lifecycle(other_key), "retired")

        self.client.process_at_most = None
        resumed = self.clean("PAYEMS", "UNRATE", store=store)

        self.assertTrue(resumed.succeeded)
        self.assertEqual((resumed.claimed, resumed.acknowledged_deletes), (1, 32))

    def test_backoff_between_delete_retries_is_bounded(self):
        self.replace()
        store = self.make_store(max_attempts=4, backoff_base_seconds=0.5, backoff_cap_seconds=1.0)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.process_at_most = 0

        with (
            patch.object(dynamodb.random, "uniform", side_effect=lambda low, high: high),
            patch.object(dynamodb.time, "sleep") as sleep,
        ):
            summary = self.clean(store=store)

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0, 1.0])
        self.assertEqual(summary.failed, 1)


class InterruptionTests(CleanupTestCase):
    def test_request_failure_right_after_the_claim(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before("get_item", once(fail_with("InternalServerError", "GetItem")))

        interrupted = self.clean()

        self.assertEqual((interrupted.claimed, interrupted.swept), (1, 0))
        self.assertTrue(interrupted.stopped_early)
        self.assertFalse(interrupted.succeeded)
        (failure,) = interrupted.failures
        self.assertEqual(failure.reason, "DynamoDB get_item failed (InternalServerError)")
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertEqual(len(self.local.vintages(retired_key)), 2)

        resumed = self.clean()

        self.assertTrue(resumed.succeeded)
        self.assertEqual((resumed.claimed, resumed.swept, resumed.acknowledged_deletes), (0, 1, 2))

    def test_request_failure_part_way_through_deletion(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        seen = []

        def fail_second_batch(parameters):
            seen.append(1)
            if len(seen) == 2:
                fail_with("InternalServerError", "BatchWriteItem")(parameters)

        self.client.before("batch_write_item", fail_second_batch)

        interrupted = self.clean()

        self.assertEqual((interrupted.claimed, interrupted.swept, interrupted.failed), (1, 0, 1))
        self.assertEqual(interrupted.acknowledged_deletes, 25)
        self.assertEqual(interrupted.failures[0].acknowledged_deletes, 25)
        self.assertFalse(interrupted.succeeded)
        self.assertEqual(len(self.local.vintages(retired_key)), 35)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        resumed = self.clean()

        self.assertEqual((resumed.claimed, resumed.swept, resumed.acknowledged_deletes), (0, 1, 35))
        self.assertEqual(self.local.vintages(retired_key), [])
        self.assertEqual(resumed.deferred, 1)

        self.clock.advance(STABILIZATION_PERIOD)
        self.assertEqual(self.clean().completed, 1)

    def test_process_stopping_mid_pass_can_be_run_again(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        seen = []

        def crash(parameters):
            seen.append(1)
            if len(seen) == 2:
                raise SystemExit("worker stopped")

        self.client.before("batch_write_item", crash)

        with self.assertRaises(SystemExit):
            self.clean()

        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertEqual(len(self.local.vintages(retired_key)), 35)

        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.clean()

        self.assertIsNone(self.record(retired_key))
        self.assertEqual(self.local.vintages(retired_key), [])
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(3, base=2000.0),
        )

    def test_process_stopping_before_finalization_can_be_run_again(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)

        def crash(parameters):
            raise SystemExit("worker stopped")

        self.client.before("transact_write_items", once(crash))

        with self.assertRaises(SystemExit):
            self.clean()
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        self.assertEqual(self.clean().completed, 1)
        self.assertIsNone(self.record(retired_key))

    def test_discovery_failure_is_reported_and_stops_the_pass(self):
        self.replace()
        self.replace(series_id="UNRATE")
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before("query", once(fail_with("AccessDeniedException", "Query")))
        self.client.calls.clear()

        summary = self.clean("PAYEMS", "UNRATE")

        self.assertEqual(summary.examined, 0)
        self.assertTrue(summary.stopped_early)
        self.assertFalse(summary.succeeded)
        (failure,) = summary.failures
        self.assertEqual(
            (failure.series_id, failure.record, failure.reason),
            ("PAYEMS", None, "DynamoDB query failed (AccessDeniedException)"),
        )
        self.assertEqual(self.client.operations(), ["query"])

    def test_failures_do_not_repeat_aws_details(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before(
            "batch_write_item",
            once(fail_with("AccessDeniedException", "BatchWriteItem")),
        )

        (failure,) = self.clean().failures

        for private in ("arn:aws", "123456789012", TABLE_NAME):
            self.assertNotIn(private, failure.reason)

    def test_read_only_store_cannot_clean(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()

        summary = self.clean(store=self.make_store(read_only=True))

        self.assertTrue(summary.stopped_early)
        self.assertIn("read-only", summary.failures[0].reason)
        self.assertEqual(self.client.operations(), ["query"])
        self.assertEqual(self.lifecycle(retired_key), "retired")

    def test_unexpected_errors_are_not_swallowed(self):
        self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)

        def bug(parameters):
            raise KeyError("bug")

        self.client.before("batch_write_item", once(bug))

        with self.assertRaises(KeyError):
            self.clean()


class StabilizationTests(CleanupTestCase):
    def claimed_and_swept(self, **where):
        retired_key = self.replace(**where)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean(where.get("series_id", "PAYEMS"))
        self.assertEqual(self.local.vintages(retired_key), [])
        return retired_key

    def test_cleanup_record_is_kept_until_the_stabilization_period_has_passed(self):
        retired_key = self.claimed_and_swept()
        self.clock.advance(JUST_UNDER_STABILIZATION)
        self.client.calls.clear()

        early = self.clean()

        self.assertEqual((early.swept, early.deferred, early.completed), (1, 1, 0))
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertNotIn("transact_write_items", self.client.operations())

        self.clock.advance(timedelta(seconds=1))
        on_time = self.clean()

        self.assertEqual((on_time.deferred, on_time.completed), (0, 1))
        self.assertIsNone(self.record(retired_key))

    def test_one_pass_never_claims_and_finalizes_the_same_generation(self):
        retired_key = self.replace()
        self.clock.advance(timedelta(days=30))

        summary = self.clean()

        self.assertEqual((summary.claimed, summary.completed, summary.deferred), (1, 0, 1))
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_empty_generation_claimed_long_after_its_deadline_still_waits(self):
        abandoned_key = self.abandon(at_batch=1)
        self.clock.advance(timedelta(days=30))
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual((summary.claimed, summary.acknowledged_deletes), (1, 0))
        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        # The only transaction is the claim; finalization is not attempted.
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)
        self.assertEqual(self.lifecycle(abandoned_key), "deleting")

    def test_stabilization_is_measured_from_the_claim_not_the_deadline(self):
        retired_key = self.replace()
        self.clock.advance(timedelta(days=30))
        self.clean()
        self.clock.advance(JUST_UNDER_STABILIZATION)

        self.assertEqual(self.clean().completed, 0)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_late_items_are_deleted_and_the_record_is_kept_for_another_pass(self):
        retired_key = self.claimed_and_swept()
        self.put_late_items(retired_key, count=3)
        self.clock.advance(STABILIZATION_PERIOD)

        second = self.clean()

        self.assertEqual((second.swept, second.acknowledged_deletes), (1, 3))
        self.assertEqual((second.deferred, second.completed), (1, 0))
        self.assertEqual(self.local.vintages(retired_key), [])
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        third = self.clean()

        self.assertEqual((third.acknowledged_deletes, third.completed), (0, 1))
        self.assertIsNone(self.record(retired_key))

    def test_late_items_during_stabilization_are_swept_on_every_pass(self):
        retired_key = self.claimed_and_swept()
        for _ in range(3):
            self.put_late_items(retired_key, count=2)
            self.clock.advance(timedelta(minutes=5))

            summary = self.clean()

            self.assertEqual((summary.acknowledged_deletes, summary.completed), (2, 0))
            self.assertEqual(self.local.vintages(retired_key), [])
            self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_items_arriving_between_the_sweep_and_finalization_keep_the_record(self):
        retired_key = self.claimed_and_swept()
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.calls.clear()
        queries = []

        def arrive_after_sweep(parameters):
            queries.append(1)
            if len(queries) == 3:
                self.put_late_items(retired_key, count=1)

        self.client.before("query", arrive_after_sweep)

        summary = self.clean()

        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertNotIn("transact_write_items", self.client.operations())
        self.assertEqual(len(self.local.vintages(retired_key)), 1)

    def test_deadline_expiring_during_empty_check_prevents_finalization(self):
        retired_key = self.claimed_and_swept()
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.calls.clear()
        generation_queries = []

        def expire_during_final_check(parameters):
            partition = parameters["ExpressionAttributeValues"][":pk"]["S"]
            if "#GEN#" in partition:
                generation_queries.append(1)
                if len(generation_queries) == 2:
                    self.clock.advance(timedelta(seconds=70))

        self.client.before("query", expire_during_final_check)

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual(summary.completed, 0)
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertNotIn("transact_write_items", self.client.operations())

    def test_minimum_stabilization_period_is_accepted_and_respected(self):
        retired_key = self.claimed_and_swept()
        period = self.store.in_flight_write_allowance + CLOCK_SKEW_ALLOWANCE
        self.clock.advance(period - timedelta(seconds=1))

        self.assertEqual(self.clean(stabilization_period=period).completed, 0)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        self.clock.advance(timedelta(seconds=1))

        self.assertEqual(self.clean(stabilization_period=period).completed, 1)
        self.assertIsNone(self.record(retired_key))

    def test_longer_stabilization_period_can_be_configured(self):
        retired_key = self.claimed_and_swept()
        period = timedelta(hours=2)
        self.clock.advance(STABILIZATION_PERIOD)

        self.assertEqual(self.clean(stabilization_period=period).completed, 0)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        self.clock.advance(period)

        self.assertEqual(self.clean(stabilization_period=period).completed, 1)

    def test_minimum_period_covers_an_in_flight_write_and_clock_skew(self):
        # A late writer stops sending once its deadline passes, so only a
        # request already in flight can land after the claim. Its worst case
        # is every SDK attempt timing out, with the SDK's longest wait (twenty
        # seconds in standard retry mode), plus the SDK's retry-after
        # allowance, between attempts.
        config = dynamodb._create_client(
            region_name=dynamodb_support.REGION,
            endpoint_url=None,
            client_config=None,
        ).meta.config
        attempts = config.retries["total_max_attempts"]
        in_flight = attempts * timedelta(
            seconds=config.connect_timeout + config.read_timeout
        ) + (attempts - 1) * timedelta(seconds=25)

        self.assertLessEqual(in_flight, IN_FLIGHT_WRITE_ALLOWANCE)
        self.assertEqual(
            MINIMUM_STABILIZATION_PERIOD,
            IN_FLIGHT_WRITE_ALLOWANCE + CLOCK_SKEW_ALLOWANCE,
        )
        self.assertGreaterEqual(CLOCK_SKEW_ALLOWANCE, timedelta(minutes=10))
        self.assertGreater(STABILIZATION_PERIOD, MINIMUM_STABILIZATION_PERIOD)

    def test_custom_client_timing_raises_the_required_stabilization_period(self):
        store = self.make_store(in_flight_write_allowance=timedelta(hours=1))

        with self.assertRaisesRegex(ValueError, "1:30:00"):
            self.clean(store=store)

        self.assertEqual(self.client.calls, [])

    def test_writer_stops_staging_once_its_cleanup_deadline_passes(self):
        self.store.save_history(history())
        seen = []

        def take_too_long(parameters):
            seen.append(1)
            if len(seen) == 2:
                self.clock.advance(GENERATION_GRACE_PERIOD)

        self.client.before("batch_write_item", take_too_long)
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBStoreError, "grace period"):
            self.store.save_history(history(80, base=2000.0))

        self.assertEqual(len(self.client.parameters("batch_write_item")), 2)
        self.assertNotIn("transact_write_items", self.client.operations())
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(),
        )
        (staged,) = [
            item for item in self.local.cleanup_records() if item["lifecycle"]["S"] == "staged"
        ]
        self.assertEqual(len(self.local.vintages(staged["generation_key"]["S"])), 50)

    def test_writer_does_not_resend_unprocessed_items_after_its_deadline(self):
        self.client.process_at_most = 1

        def take_too_long(parameters):
            self.clock.advance(GENERATION_GRACE_PERIOD)

        self.client.before("batch_write_item", once(take_too_long))

        with self.assertRaisesRegex(DynamoDBStoreError, "grace period"):
            self.store.save_history(history(3))

        self.assertEqual(len(self.client.parameters("batch_write_item")), 1)

    def test_cleanup_running_while_a_slow_writer_is_about_to_publish(self):
        self.store.save_history(history())
        active_key = self.active_key()
        worker = DynamoDBRevisionStore(TABLE_NAME, client=self.local.client)
        passes = []

        def clean_before_publication(parameters):
            self.clock.advance(GENERATION_GRACE_PERIOD)
            passes.append(self.clean(store=worker))

        self.client.before("transact_write_items", once(clean_before_publication))

        with self.assertRaisesRegex(DynamoDBStoreError, "claimed for cleanup"):
            self.store.save_history(history(40, base=2000.0))

        (summary,) = passes
        self.assertEqual((summary.claimed, summary.acknowledged_deletes), (1, 40))
        self.assertEqual(self.active_key(), active_key)
        self.assertEqual(
            self.store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(),
        )
        self.assertEqual(len(self.local.vintages()), 2)


class ManifestProtectionTests(CleanupTestCase):
    def deleting_generation(self):
        retired_key = self.replace(old_count=30)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.assertTrue(
            self.store.claim_generation_for_cleanup(
                **self.generation_arguments(retired_key),
                now=self.clock(),
            )
        )
        return retired_key

    def test_generation_the_manifest_comes_to_reference_is_never_deleted(self):
        retired_key = self.deleting_generation()
        other_key = self.replace(series_id="UNRATE")
        self.point_manifest_at(retired_key)
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.calls.clear()

        summary = self.clean("PAYEMS", "UNRATE")

        self.assertFalse(summary.succeeded)
        self.assertFalse(summary.stopped_early)
        (failure,) = summary.failures
        self.assertEqual(failure.record, self.record(retired_key)["sk"]["S"])
        self.assertIn("manifest references", failure.reason)
        self.assertEqual(len(self.local.vintages(retired_key)), 30)
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        deleted_partitions = {
            request["DeleteRequest"]["Key"]["pk"]["S"]
            for parameters in self.client.parameters("batch_write_item")
            for request in parameters["RequestItems"][TABLE_NAME]
        }
        self.assertEqual(deleted_partitions, {other_key})
        self.assertEqual(self.lifecycle(other_key), "deleting")

    def test_manifest_that_cannot_be_read_blocks_deletion(self):
        retired_key = self.deleting_generation()
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={"pk": {"S": "SERIES#PAYEMS"}, "sk": {"S": "OBS#2008-08-01"}},
        )
        self.client.calls.clear()

        summary = self.clean()

        self.assertEqual(summary.failed, 1)
        self.assertEqual(len(self.local.vintages(retired_key)), 30)
        self.assertNotIn("batch_write_item", self.client.operations())

    def test_generation_without_a_manifest_can_be_cleaned(self):
        abandoned_key = self.abandon()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)

        self.assertEqual(self.clean().completed, 1)
        self.assertIsNone(self.record(abandoned_key))
        self.assertEqual(self.local.manifests(), [])

    def test_finalization_fails_when_the_manifest_changes_after_the_sweep(self):
        retired_key = self.deleting_generation()
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.before(
            "transact_write_items",
            once(lambda parameters: self.point_manifest_at(retired_key)),
        )

        summary = self.clean()

        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        later = self.clean()

        self.assertEqual(later.failed, 1)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_finalization_fails_when_the_record_changes_after_the_sweep(self):
        retired_key = self.deleting_generation()
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)

        def reclaim(parameters):
            self.local.client.put_item(
                TableName=TABLE_NAME,
                Item={**self.record(retired_key), "claimed_at": {"S": "2026-06-01T00:00:00Z"}},
            )

        self.client.before("transact_write_items", once(reclaim))

        summary = self.clean()

        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertEqual(self.record(retired_key)["claimed_at"], {"S": "2026-06-01T00:00:00Z"})

    def test_record_finalized_by_another_worker_is_not_an_error(self):
        retired_key = self.deleting_generation()
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        other = DynamoDBRevisionStore(TABLE_NAME, client=self.local.client)

        def finish_first(parameters):
            clean_up_generations(other, ("PAYEMS",), clock=self.clock)

        self.client.before("transact_write_items", once(finish_first))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertIsNone(self.record(retired_key))

    def test_record_removed_between_listing_and_sweeping_is_deferred(self):
        retired_key = self.deleting_generation()
        sort_key = self.record(retired_key)["sk"]

        def remove(parameters):
            self.local.client.delete_item(
                TableName=TABLE_NAME,
                Key={"pk": {"S": "SERIES#PAYEMS"}, "sk": sort_key},
            )

        self.client.before("get_item", once(remove))
        self.client.calls.clear()

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.swept, summary.deferred), (0, 1))
        self.assertNotIn("batch_write_item", self.client.operations())
        self.assertEqual(len(self.local.vintages(retired_key)), 30)


class MalformedRecordTests(CleanupTestCase):
    def setUp(self):
        super().setUp()
        self.retired_key = self.replace()
        self.original = self.record(self.retired_key)
        self.clock.advance(GENERATION_GRACE_PERIOD + STABILIZATION_PERIOD)

    def rewrite(self, remove=(), **attributes):
        item = {**self.original, **attributes}
        for name in remove:
            del item[name]
        self.local.client.put_item(TableName=TABLE_NAME, Item=item)
        return item

    def assert_left_alone(self, stored, record=None):
        self.client.calls.clear()

        summary = self.clean()

        self.assertFalse(summary.succeeded)
        self.assertFalse(summary.stopped_early)
        (failure,) = summary.failures
        self.assertEqual(failure.series_id, "PAYEMS")
        self.assertEqual(failure.record, record or stored["sk"]["S"])
        self.assertEqual(failure.reason, "The cleanup record is malformed")
        self.assertIn(stored, self.local.cleanup_records())
        self.assertEqual(len(self.local.vintages(self.retired_key)), 2)
        self.assertEqual(self.client.operations(), ["query"])

    def test_record_without_a_lifecycle(self):
        self.assert_left_alone(self.rewrite(remove=("lifecycle",)))

    def test_record_with_an_unknown_lifecycle(self):
        self.assert_left_alone(self.rewrite(lifecycle={"S": "expired"}))

    def test_record_with_a_non_string_lifecycle(self):
        self.assert_left_alone(self.rewrite(lifecycle={"N": "1"}))

    def test_retired_record_without_a_deadline(self):
        self.assert_left_alone(self.rewrite(remove=("delete_after",)))

    def test_record_with_an_unreadable_deadline(self):
        for value in ({"S": "soon"}, {"S": "2026-01-01"}, {"N": "1767268800"}):
            with self.subTest(value=value):
                self.assert_left_alone(self.rewrite(delete_after=value))

    def test_deleting_record_without_a_claim_time(self):
        self.assert_left_alone(self.rewrite(lifecycle={"S": "deleting"}))

    def test_deleting_record_with_an_unreadable_claim_time(self):
        self.assert_left_alone(
            self.rewrite(lifecycle={"S": "deleting"}, claimed_at={"S": "yesterday"})
        )

    def test_record_naming_another_generation(self):
        other = self.active_key()

        self.assert_left_alone(self.rewrite(generation_key={"S": other}))

        self.assertEqual(len(self.local.vintages(other)), 3)

    def test_record_without_a_generation_key(self):
        self.assert_left_alone(self.rewrite(remove=("generation_key",)))

    def test_record_with_an_unexpected_sort_key(self):
        for sort_key in (
            "RETIRE#2008-8-1#" + "0" * 32,
            "RETIRE#2008-08-01#not-a-generation",
            "RETIRE#2008-08-01",
            "RETIRE#2008-08-01#" + "0" * 32 + "#extra",
        ):
            with self.subTest(sort_key=sort_key):
                stored = {**self.original, "sk": {"S": sort_key}}
                self.local.client.put_item(TableName=TABLE_NAME, Item=stored)

                summary = self.clean()

                self.assertIn(sort_key, [failure.record for failure in summary.failures])
                self.assertIn(stored, self.local.cleanup_records())
                self.local.client.delete_item(
                    TableName=TABLE_NAME,
                    Key={"pk": stored["pk"], "sk": stored["sk"]},
                )

    def test_other_records_are_still_cleaned(self):
        self.rewrite(lifecycle={"S": "expired"})
        other_key = self.replace(observation_date="2008-09-01")
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean()

        self.assertEqual((summary.failed, summary.claimed, summary.acknowledged_deletes), (1, 1, 2))
        self.assertEqual(self.lifecycle(other_key), "deleting")

    def test_malformed_record_is_reported_on_every_pass(self):
        stored = self.rewrite(remove=("delete_after",))

        for _ in range(3):
            self.assert_left_alone(stored)

    def test_iter_cleanup_records_marks_malformed_records(self):
        self.rewrite(remove=("lifecycle",))

        records = list(self.store.iter_cleanup_records(series_id="PAYEMS"))

        malformed = [record for record in records if isinstance(record, MalformedCleanupRecord)]
        self.assertEqual(
            malformed,
            [MalformedCleanupRecord(series_id="PAYEMS", sort_key=self.original["sk"]["S"])],
        )
        self.assertEqual(len(records), 2)


def cancel(parameters):
    raise client_error(
        "TransactionCanceledException",
        "TransactWriteItems",
        "Cancelled [ThrottlingError] arn:aws:dynamodb:us-east-2:123456789012:table/private",
    )


class TransactionCancellationTests(CleanupTestCase):
    """A cancelled claim or finalization is a deferral only if its conditions changed."""

    def due(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()
        return retired_key

    def ready_to_finalize(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        self.clock.advance(STABILIZATION_PERIOD)
        self.client.calls.clear()
        return retired_key

    def transactions(self):
        return len(self.client.parameters("transact_write_items"))

    def test_claim_cancelled_while_still_eligible_is_retried(self):
        retired_key = self.due()
        self.client.before("transact_write_items", once(cancel))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.claimed, summary.acknowledged_deletes), (1, 2))
        self.assertEqual(self.transactions(), 2)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_claim_cancelled_on_every_attempt_fails_the_pass(self):
        retired_key = self.due()
        other_key = self.replace(series_id="UNRATE")
        self.clock.advance(GENERATION_GRACE_PERIOD)
        store = self.make_store(max_attempts=4)
        self.client.before("transact_write_items", cancel)
        self.client.calls.clear()

        summary = self.clean("PAYEMS", "UNRATE", store=store)

        self.assertEqual(self.transactions(), 4)
        self.assertFalse(summary.succeeded)
        self.assertTrue(summary.stopped_early)
        self.assertEqual((summary.claimed, summary.deferred, summary.swept), (0, 0, 0))
        (failure,) = summary.failures
        self.assertEqual(failure.record, self.record(retired_key)["sk"]["S"])
        self.assertIn("kept cancelling a cleanup transaction after 4 attempts", failure.reason)
        for private in ("arn:aws", "123456789012", "ThrottlingError", TABLE_NAME):
            self.assertNotIn(private, failure.reason)
        self.assertEqual(self.lifecycle(retired_key), "retired")
        self.assertEqual(self.lifecycle(other_key), "retired")
        self.assertEqual(len(self.local.vintages()), 10)
        self.assertNotIn("batch_write_item", self.client.operations())

    def test_claim_raises_a_storage_error_when_cancellations_never_stop(self):
        retired_key = self.due()
        store = self.make_store(max_attempts=3)
        self.client.before("transact_write_items", cancel)

        with self.assertRaisesRegex(DynamoDBRetryExhaustedError, "3 attempts") as raised:
            store.claim_generation_for_cleanup(
                **self.generation_arguments(retired_key),
                now=self.clock(),
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.transactions(), 3)

    def test_claim_retries_wait_no_longer_than_the_backoff_cap(self):
        retired_key = self.due()
        store = self.make_store(max_attempts=5, backoff_base_seconds=0.5, backoff_cap_seconds=2.0)
        self.client.before("transact_write_items", cancel)

        with (
            patch.object(dynamodb.random, "uniform", side_effect=lambda low, high: high),
            patch.object(dynamodb.time, "sleep") as sleep,
        ):
            with self.assertRaises(DynamoDBRetryExhaustedError):
                store.claim_generation_for_cleanup(
                    **self.generation_arguments(retired_key),
                    now=self.clock(),
                )

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0, 2.0, 2.0])

    def test_claim_applied_despite_a_reported_cancellation_counts_as_claimed(self):
        retired_key = self.due()
        target = self.local.client.transact_write_items

        def commit_then_cancel(parameters):
            target(**parameters)
            cancel(parameters)

        self.client.before("transact_write_items", once(commit_then_cancel))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.claimed, summary.acknowledged_deletes), (1, 2))
        self.assertEqual(self.transactions(), 1)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_claim_lost_to_another_worker_is_an_ordinary_deferral(self):
        retired_key = self.due()
        other = DynamoDBRevisionStore(TABLE_NAME, client=self.local.client)

        def claim_first(parameters):
            other.claim_generation_for_cleanup(
                **self.generation_arguments(retired_key),
                now=self.clock() + timedelta(seconds=1),
            )

        self.client.before("transact_write_items", once(claim_first))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.claimed, summary.deferred, summary.swept), (0, 1, 0))
        self.assertEqual(self.transactions(), 1)
        self.assertEqual(len(self.local.vintages(retired_key)), 2)

    def test_claim_refused_by_its_conditions_is_not_retried(self):
        retired_key = self.replace()
        active_key = self.active_key()
        store = self.make_store(max_attempts=5)
        self.client.calls.clear()

        not_due = store.claim_generation_for_cleanup(
            **self.generation_arguments(retired_key),
            now=self.clock(),
        )
        active = store.claim_generation_for_cleanup(
            **self.generation_arguments(active_key),
            now=self.clock() + timedelta(days=1),
        )
        unknown = store.claim_generation_for_cleanup(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            generation="0" * 32,
            now=self.clock() + timedelta(days=1),
        )

        self.assertEqual((not_due, active, unknown), (False, False, False))
        self.assertEqual(self.transactions(), 3)

    def test_claim_of_a_due_record_the_manifest_references_is_not_retried(self):
        retired_key = self.due()
        self.point_manifest_at(retired_key)
        store = self.make_store(max_attempts=5)

        claimed = store.claim_generation_for_cleanup(
            **self.generation_arguments(retired_key),
            now=self.clock(),
        )

        self.assertFalse(claimed)
        self.assertEqual(self.transactions(), 1)

    def test_finalization_cancelled_while_still_valid_is_retried(self):
        retired_key = self.ready_to_finalize()
        self.client.before("transact_write_items", once(cancel))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(self.transactions(), 2)
        self.assertIsNone(self.record(retired_key))

    def test_finalization_cancelled_on_every_attempt_fails_the_pass(self):
        retired_key = self.ready_to_finalize()
        store = self.make_store(max_attempts=3)
        self.client.before("transact_write_items", cancel)

        summary = self.clean(store=store)

        self.assertEqual(self.transactions(), 3)
        self.assertFalse(summary.succeeded)
        self.assertTrue(summary.stopped_early)
        self.assertEqual((summary.completed, summary.deferred, summary.failed), (0, 0, 1))
        self.assertIn("kept cancelling", summary.failures[0].reason)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_finalization_stops_retrying_when_items_appear(self):
        retired_key = self.ready_to_finalize()
        store = self.make_store(max_attempts=5)

        def cancel_and_add_item(parameters):
            self.put_late_items(retired_key, count=1)
            cancel(parameters)

        self.client.before("transact_write_items", cancel_and_add_item)

        summary = self.clean(store=store)

        self.assertTrue(summary.succeeded)
        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertEqual(self.transactions(), 1)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

    def test_finalization_applied_despite_a_reported_cancellation_is_not_repeated(self):
        retired_key = self.ready_to_finalize()
        target = self.local.client.transact_write_items

        def commit_then_cancel(parameters):
            target(**parameters)
            cancel(parameters)

        self.client.before("transact_write_items", once(commit_then_cancel))

        summary = self.clean()

        self.assertTrue(summary.succeeded)
        self.assertEqual(self.transactions(), 1)
        self.assertIsNone(self.record(retired_key))

    def test_other_transaction_errors_are_failures(self):
        retired_key = self.due()
        self.client.before(
            "transact_write_items",
            once(fail_with("ThrottlingException", "TransactWriteItems")),
        )

        summary = self.clean()

        self.assertFalse(summary.succeeded)
        self.assertTrue(summary.stopped_early)
        self.assertEqual(
            summary.failures[0].reason,
            "DynamoDB transact_write_items failed (ThrottlingException)",
        )
        self.assertEqual(self.lifecycle(retired_key), "retired")


class SweepTests(CleanupTestCase):
    def claimed(self, count=60):
        retired_key = self.replace(old_count=count)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        arguments = self.generation_arguments(retired_key)
        self.assertTrue(self.store.claim_generation_for_cleanup(**arguments, now=self.clock()))
        self.client.calls.clear()
        return retired_key, arguments

    def test_deletes_are_sent_while_the_partition_is_still_being_read(self):
        retired_key, arguments = self.claimed(60)
        self.client.page_limit = 10

        result = self.store.sweep_generation(**arguments)

        self.assertEqual(result, SweepResult(acknowledged=60, complete=True))
        operations = [
            name for name in self.client.operations() if name in ("query", "batch_write_item")
        ]
        first_delete = operations.index("batch_write_item")
        self.assertIn("query", operations[first_delete:])
        self.assertLessEqual(first_delete, 3)
        for parameters in self.client.parameters("batch_write_item"):
            self.assertLessEqual(len(parameters["RequestItems"][TABLE_NAME]), 25)
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_sweep_stops_at_its_item_limit_and_reports_what_is_left(self):
        retired_key, arguments = self.claimed(60)

        first = self.store.sweep_generation(**arguments, max_items=30)

        self.assertEqual(first, SweepResult(acknowledged=30, complete=False))
        self.assertEqual(len(self.local.vintages(retired_key)), 30)

        second = self.store.sweep_generation(**arguments, max_items=30)

        self.assertEqual(second, SweepResult(acknowledged=30, complete=True))
        self.assertEqual(self.local.vintages(retired_key), [])
        self.assertEqual(
            self.store.sweep_generation(**arguments, max_items=30),
            SweepResult(acknowledged=0, complete=True),
        )

    def test_sweep_with_no_allowance_only_reports_whether_items_exist(self):
        retired_key, arguments = self.claimed(3)

        occupied = self.store.sweep_generation(**arguments, max_items=0)

        self.assertEqual(occupied, SweepResult(acknowledged=0, complete=False))
        self.assertEqual(len(self.local.vintages(retired_key)), 3)
        self.assertNotIn("batch_write_item", self.client.operations())

        self.store.sweep_generation(**arguments)

        self.assertEqual(
            self.store.sweep_generation(**arguments, max_items=0),
            SweepResult(acknowledged=0, complete=True),
        )

    def test_sweep_rejects_an_invalid_item_limit(self):
        _, arguments = self.claimed(3)

        for max_items in (-1, 1.5, True, "3"):
            with self.subTest(max_items=max_items):
                with self.assertRaisesRegex(ValueError, "max_items"):
                    self.store.sweep_generation(**arguments, max_items=max_items)

        self.assertEqual(self.client.calls, [])

    def test_failed_batch_reports_the_deletes_acknowledged_before_it(self):
        retired_key, arguments = self.claimed(60)
        seen = []

        def fail_third_batch(parameters):
            seen.append(1)
            if len(seen) == 3:
                fail_with("InternalServerError", "BatchWriteItem")(parameters)

        self.client.before("batch_write_item", fail_third_batch)

        with self.assertRaises(DynamoDBSweepError) as raised:
            self.store.sweep_generation(**arguments)

        self.assertEqual(raised.exception.acknowledged, 50)
        self.assertEqual(
            str(raised.exception),
            "DynamoDB batch_write_item failed (InternalServerError)",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(len(self.local.vintages(retired_key)), 10)

    def test_request_error_after_a_partial_response_preserves_progress(self):
        retired_key, arguments = self.claimed(60)
        calls = []

        def accept_ten_then_fail(parameters):
            calls.append(1)
            if len(calls) == 1:
                self.client.process_at_most = 10
            else:
                fail_with("InternalServerError", "BatchWriteItem")(parameters)

        self.client.before("batch_write_item", accept_ten_then_fail)

        with self.assertRaises(DynamoDBSweepError) as raised:
            self.store.sweep_generation(**arguments)

        self.assertEqual(raised.exception.acknowledged, 10)
        self.assertEqual(len(self.local.vintages(retired_key)), 50)

    def test_exhausted_retries_report_the_deletes_that_were_accepted(self):
        retired_key, arguments = self.claimed(60)
        store = self.make_store(max_attempts=3)
        calls = []

        def accept_less_each_time(parameters):
            calls.append(1)
            # First batch: 25 accepted. Second batch: 10, then 4, then none.
            self.client.process_at_most = {1: None, 2: 10, 3: 4}.get(len(calls), 0)

        self.client.before("batch_write_item", accept_less_each_time)

        with self.assertRaises(DynamoDBSweepError) as raised:
            store.sweep_generation(**arguments)

        self.assertEqual(raised.exception.acknowledged, 25 + 10 + 4)
        self.assertIn("unprocessed after 3 attempts", str(raised.exception))
        self.assertEqual(len(self.local.vintages(retired_key)), 60 - 39)

    def test_failed_page_read_reports_the_deletes_acknowledged_before_it(self):
        retired_key, arguments = self.claimed(60)
        self.client.page_limit = 30
        queries = []

        def fail_second_page(parameters):
            queries.append(1)
            if len(queries) == 2:
                fail_with("InternalServerError", "Query")(parameters)

        self.client.before("query", fail_second_page)

        with self.assertRaises(DynamoDBSweepError) as raised:
            self.store.sweep_generation(**arguments)

        self.assertEqual(raised.exception.acknowledged, 25)
        self.assertEqual(len(self.local.vintages(retired_key)), 35)


class BudgetTests(CleanupTestCase):
    def retire_many(self, count=7, items=2):
        dates = [f"2008-{month:02d}-01" for month in range(1, count + 1)]
        retired = [self.replace(old_count=items, observation_date=day) for day in dates]
        self.clock.advance(GENERATION_GRACE_PERIOD)
        return retired

    def lifecycles(self, generation_keys):
        return [
            self.lifecycle(key) if self.record(key) else "removed" for key in generation_keys
        ]

    def test_default_budgets_are_finite_and_stop_a_backlog(self):
        retired = [
            self.replace(
                old_count=1,
                observation_date=(date(2008, 1, 1) + timedelta(days=offset)).isoformat(),
            )
            for offset in range(DEFAULT_MAX_RECORDS + 1)
        ]
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean()

        self.assertEqual(DEFAULT_MAX_DELETES, 500)
        self.assertEqual(DEFAULT_TIME_BUDGET, timedelta(minutes=8))
        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.succeeded)
        self.assertEqual(summary.claimed, DEFAULT_MAX_RECORDS)
        self.assertEqual(
            self.lifecycles(retired),
            ["deleting"] * DEFAULT_MAX_RECORDS + ["retired"],
        )

    def test_delete_budget_stops_a_sweep_part_way_and_is_not_success(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)

        limited = self.clean(max_deletes=40)

        self.assertTrue(limited.budget_exhausted)
        self.assertFalse(limited.succeeded)
        self.assertFalse(limited.stopped_early)
        self.assertEqual(limited.failures, ())
        self.assertEqual(
            (limited.claimed, limited.swept, limited.acknowledged_deletes, limited.deferred),
            (1, 0, 40, 1),
        )
        self.assertEqual(len(self.local.vintages(retired_key)), 20)
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        resumed = self.clean(max_deletes=40)

        self.assertFalse(resumed.budget_exhausted)
        self.assertTrue(resumed.succeeded)
        self.assertEqual((resumed.claimed, resumed.swept, resumed.acknowledged_deletes), (0, 1, 20))
        self.assertEqual(self.local.vintages(retired_key), [])

    def test_generation_is_not_finalized_after_a_budget_limited_sweep(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.store.claim_generation_for_cleanup(
            **self.generation_arguments(retired_key),
            now=self.clock(),
        )
        self.clock.advance(timedelta(days=30))
        self.client.calls.clear()

        summary = self.clean(max_deletes=10)

        self.assertEqual((summary.completed, summary.deferred), (0, 1))
        self.assertTrue(summary.budget_exhausted)
        self.assertNotIn("transact_write_items", self.client.operations())
        self.assertEqual(self.lifecycle(retired_key), "deleting")
        self.assertEqual(len(self.local.vintages(retired_key)), 50)

    def test_delete_budget_is_shared_by_the_whole_pass(self):
        first_key, second_key, third_key = self.retire_many(count=3, items=30)

        summary = self.clean(max_deletes=45)

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual((summary.claimed, summary.swept, summary.acknowledged_deletes), (2, 1, 45))
        self.assertEqual(self.local.vintages(first_key), [])
        self.assertEqual(len(self.local.vintages(second_key)), 15)
        self.assertEqual(len(self.local.vintages(third_key)), 30)
        self.assertEqual(
            self.lifecycles([first_key, second_key, third_key]),
            ["deleting", "deleting", "retired"],
        )

    def test_delete_budget_that_is_exactly_enough_is_not_exhausted(self):
        self.replace(old_count=30)
        self.clock.advance(GENERATION_GRACE_PERIOD)

        summary = self.clean(max_deletes=30)

        self.assertFalse(summary.budget_exhausted)
        self.assertEqual((summary.swept, summary.acknowledged_deletes), (1, 30))

    def test_record_budget_limits_new_claims(self):
        retired = self.retire_many(count=7)

        summary = self.clean(max_records=3)

        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.succeeded)
        self.assertEqual((summary.claimed, summary.swept, summary.acknowledged_deletes), (3, 3, 6))
        self.assertEqual(self.lifecycles(retired), ["deleting"] * 3 + ["retired"] * 4)
        self.assertEqual(len(self.local.vintages(retired[3])), 2)

    def test_skipped_records_do_not_use_the_record_budget(self):
        retired = self.retire_many(count=3)
        for month in range(1, 4):
            self.store.save_history(history(observation_date=f"2007-{month:02d}-01"))

        summary = self.clean(max_records=3)

        self.assertFalse(summary.budget_exhausted)
        self.assertEqual((summary.skipped, summary.claimed), (6, 3))
        self.assertEqual(self.lifecycles(retired), ["deleting"] * 3)

    def test_empty_generations_waiting_to_stabilize_do_not_use_the_record_budget(self):
        retired = self.retire_many(count=7)

        passes = [self.clean(max_records=3) for _ in range(3)]

        self.assertEqual([summary.claimed for summary in passes], [3, 3, 1])
        self.assertEqual(
            [summary.budget_exhausted for summary in passes],
            [True, True, False],
        )
        self.assertEqual(self.lifecycles(retired), ["deleting"] * 7)

    def test_resumed_deletion_uses_the_record_budget(self):
        retired = self.retire_many(count=3, items=2)
        for generation_key in retired:
            self.store.claim_generation_for_cleanup(
                **self.generation_arguments(generation_key),
                now=self.clock(),
            )

        summary = self.clean(max_records=2)

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual((summary.swept, summary.acknowledged_deletes), (2, 4))
        self.assertEqual(len(self.local.vintages(retired[2])), 2)

    def test_finalization_still_happens_when_the_record_budget_is_spent(self):
        early = self.replace(observation_date="2008-01-01")
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.clean()
        late = [self.replace(observation_date=f"2008-{month:02d}-01") for month in (2, 3)]
        self.clock.advance(STABILIZATION_PERIOD)

        summary = self.clean(max_records=1)

        self.assertEqual((summary.completed, summary.claimed), (1, 1))
        self.assertTrue(summary.budget_exhausted)
        self.assertIsNone(self.record(early))
        self.assertEqual(self.lifecycles(late), ["deleting", "retired"])

    def test_time_budget_stops_the_pass_between_records(self):
        retired = self.retire_many(count=7)
        self.client.before(
            "batch_write_item",
            lambda parameters: self.clock.advance(timedelta(seconds=40)),
        )

        summary = self.clean(time_budget=timedelta(seconds=100))

        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.succeeded)
        self.assertFalse(summary.stopped_early)
        self.assertEqual(summary.failures, ())
        self.assertEqual((summary.claimed, summary.swept), (3, 3))
        self.assertLess(summary.examined, 14)
        self.assertEqual(self.lifecycles(retired), ["deleting"] * 3 + ["retired"] * 4)

    def test_time_budget_stops_between_delete_batches(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before(
            "batch_write_item",
            lambda parameters: self.clock.advance(timedelta(seconds=40)),
        )

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.stopped_early)
        self.assertEqual(summary.acknowledged_deletes, 50)
        self.assertEqual(len(self.local.vintages(retired_key)), 10)

    def test_time_budget_stops_between_query_pages(self):
        retired_key = self.replace(old_count=60)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.page_limit = 10
        generation_queries = []

        def slow_generation_page(parameters):
            partition = parameters["ExpressionAttributeValues"][":pk"]["S"]
            if "#GEN#" in partition:
                generation_queries.append(1)
                self.clock.advance(timedelta(seconds=40))

        self.client.before("query", slow_generation_page)

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual(len(generation_queries), 2)
        self.assertEqual(summary.acknowledged_deletes, 0)
        self.assertEqual(len(self.local.vintages(retired_key)), 60)

    def test_time_budget_stops_before_a_delete_retry(self):
        retired_key = self.replace(old_count=25)
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.process_at_most = 10
        self.client.before(
            "batch_write_item",
            lambda parameters: self.clock.advance(timedelta(seconds=70)),
        )

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.stopped_early)
        self.assertEqual(summary.acknowledged_deletes, 10)
        self.assertEqual(len(self.local.vintages(retired_key)), 15)

    def test_time_budget_stops_before_later_series(self):
        self.replace()
        other_key = self.replace(series_id="UNRATE")
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.before(
            "batch_write_item",
            lambda parameters: self.clock.advance(timedelta(minutes=5)),
        )

        summary = self.clean("PAYEMS", "UNRATE", time_budget=timedelta(minutes=1))

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual(summary.claimed, 1)
        self.assertEqual(self.lifecycle(other_key), "retired")

    def test_time_budget_stops_between_discovery_pages(self):
        retired = self.retire_many(count=5)
        self.client.page_limit = 2
        discovery_pages = []

        def slow_discovery_page(parameters):
            prefix = parameters["ExpressionAttributeValues"].get(":prefix")
            if prefix == {"S": "RETIRE#"}:
                discovery_pages.append(1)
                self.clock.advance(timedelta(seconds=40))

        self.client.before("query", slow_discovery_page)

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertEqual(len(discovery_pages), 2)
        self.assertEqual(summary.examined, 2)
        self.assertEqual(
            self.lifecycles(retired),
            ["deleting", "retired", "retired", "retired", "retired"],
        )

    def test_time_budget_stops_claim_retries(self):
        retired_key = self.replace()
        self.clock.advance(GENERATION_GRACE_PERIOD)
        self.client.calls.clear()

        def expire_and_cancel(parameters):
            self.clock.advance(timedelta(seconds=70))
            fail_with("TransactionCanceledException", "TransactWriteItems")(parameters)

        self.client.before("transact_write_items", expire_and_cancel)

        summary = self.clean(time_budget=timedelta(seconds=60))

        self.assertTrue(summary.budget_exhausted)
        self.assertFalse(summary.stopped_early)
        self.assertEqual(summary.claimed, 0)
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)
        self.assertEqual(self.lifecycle(retired_key), "retired")

    def test_generous_budgets_do_not_change_the_result(self):
        retired = self.retire_many(count=3)

        summary = self.clean(
            max_records=100,
            max_deletes=1000,
            time_budget=timedelta(minutes=10),
        )

        self.assertTrue(summary.succeeded)
        self.assertFalse(summary.budget_exhausted)
        self.assertEqual((summary.claimed, summary.swept), (3, 3))
        self.assertEqual(self.lifecycles(retired), ["deleting"] * 3)

    def test_budgeted_passes_eventually_remove_everything(self):
        retired = self.retire_many(count=5, items=30)
        expected = {
            day: self.store.load_history(series_id="PAYEMS", observation_date=day)
            for day in self.store.list_observations(series_id="PAYEMS")
        }

        passes = 0
        while any(self.record(key) for key in retired):
            passes += 1
            self.assertLess(passes, 40)
            self.clean(max_records=2, max_deletes=35)
            self.clock.advance(timedelta(minutes=10))

        self.assertEqual(self.lifecycles(retired), ["removed"] * 5)
        self.assertEqual(len(self.local.cleanup_records()), 5)
        self.assertEqual(len(self.local.vintages()), 15)
        for day, stored in expected.items():
            self.assertEqual(
                self.store.load_history(series_id="PAYEMS", observation_date=day),
                stored,
            )
        settled = self.local.items()
        self.assertTrue(self.clean(max_records=2, max_deletes=35).succeeded)
        self.assertEqual(self.local.items(), settled)


class StorePrimitiveTests(CleanupTestCase):
    def test_claim_records_the_claim_time(self):
        retired_key = self.replace()
        claimed_at = START + timedelta(hours=2, seconds=5)

        self.store.claim_generation_for_cleanup(
            **self.generation_arguments(retired_key),
            now=claimed_at,
        )

        self.assertEqual(self.record(retired_key)["claimed_at"], {"S": "2026-01-01T14:00:05Z"})

    def test_sweep_refuses_a_generation_that_is_not_claimed(self):
        retired_key = self.replace()
        active_key = self.active_key()
        self.client.calls.clear()

        unknown_key = "SERIES#PAYEMS#OBS#2008-08-01#GEN#" + "0" * 32
        for generation_key in (retired_key, active_key, unknown_key):
            with self.subTest(generation_key=generation_key):
                with self.assertRaisesRegex(DynamoDBConcurrencyError, "not claimed"):
                    self.store.sweep_generation(**self.generation_arguments(generation_key))

        self.assertEqual(set(self.client.operations()), {"get_item"})
        self.assertEqual(len(self.local.vintages()), 5)

    def test_sweep_refuses_a_generation_the_manifest_references(self):
        retired_key = self.replace()
        self.store.claim_generation_for_cleanup(
            **self.generation_arguments(retired_key),
            now=START + GENERATION_GRACE_PERIOD,
        )
        self.point_manifest_at(retired_key)
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBDataError, "manifest references"):
            self.store.sweep_generation(**self.generation_arguments(retired_key))

        self.assertEqual(set(self.client.operations()), {"get_item"})
        self.assertEqual(len(self.local.vintages(retired_key)), 2)

    def test_finalize_refuses_while_items_remain_or_the_claim_differs(self):
        retired_key = self.replace()
        arguments = self.generation_arguments(retired_key)
        claimed_at = START + GENERATION_GRACE_PERIOD
        self.store.claim_generation_for_cleanup(**arguments, now=claimed_at)

        with_items = self.store.finalize_generation_cleanup(**arguments, claimed_at=claimed_at)
        self.store.sweep_generation(**arguments)
        other_claim = self.store.finalize_generation_cleanup(
            **arguments,
            claimed_at=claimed_at + timedelta(seconds=1),
        )

        self.assertEqual((with_items, other_claim), (False, False))
        self.assertEqual(self.lifecycle(retired_key), "deleting")

        self.assertTrue(self.store.finalize_generation_cleanup(**arguments, claimed_at=claimed_at))
        self.assertIsNone(self.record(retired_key))
        self.assertFalse(self.store.finalize_generation_cleanup(**arguments, claimed_at=claimed_at))

    def test_finalize_refuses_a_record_that_is_not_deleting(self):
        abandoned_key = self.abandon(at_batch=1)
        arguments = self.generation_arguments(abandoned_key)

        finalized = self.store.finalize_generation_cleanup(**arguments, claimed_at=START)

        self.assertFalse(finalized)
        self.assertEqual(self.lifecycle(abandoned_key), "staged")

    def test_primitives_validate_arguments_and_respect_read_only(self):
        reader = self.make_store(read_only=True)
        valid = {
            "series_id": "PAYEMS",
            "observation_date": "2008-08-01",
            "generation": "0" * 32,
        }
        self.client.calls.clear()

        for invalid in (
            {**valid, "series_id": "PAYEMS#X"},
            {**valid, "observation_date": "2008-8-1"},
            {**valid, "generation": "../other"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.store.sweep_generation(**invalid)
                with self.assertRaises(ValueError):
                    self.store.finalize_generation_cleanup(**invalid, claimed_at=START)
        with self.assertRaisesRegex(DynamoDBStoreError, "read-only"):
            reader.sweep_generation(**valid)
        with self.assertRaisesRegex(DynamoDBStoreError, "read-only"):
            reader.finalize_generation_cleanup(**valid, claimed_at=START)
        with self.assertRaises(ValueError):
            list(self.store.iter_cleanup_records(series_id="PAYEMS#X"))

        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
