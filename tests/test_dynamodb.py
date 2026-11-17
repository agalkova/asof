import math
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import dynamodb_support
from dynamodb_support import TABLE_NAME, InterceptingClient, client_error
from asof import (
    DynamoDBConcurrencyError,
    DynamoDBConfigurationError,
    DynamoDBDataError,
    DynamoDBRequestError,
    DynamoDBRetryExhaustedError,
    DynamoDBRevisionStore,
    DynamoDBStoreError,
    DynamoDBValueError,
    RevisionHistory,
    VintageValue,
)
from asof import dynamodb


def history(
    observation_date="2008-08-01",
    revisions=(
        VintageValue("2008-09-05", 137473.0),
        VintageValue("2008-10-03", 137477.0),
    ),
    series_id="PAYEMS",
):
    return RevisionHistory(
        series_id=series_id,
        observation_date=observation_date,
        revisions=revisions,
    )


def long_history(count, observation_date="2008-08-01"):
    first = date(2008, 9, 5).toordinal()
    return history(
        observation_date=observation_date,
        revisions=tuple(
            VintageValue(date.fromordinal(first + offset).isoformat(), 1000.0 + offset)
            for offset in range(count)
        ),
    )


REPLACEMENT = history(revisions=(VintageValue("2008-09-05", 137400.0),))


@unittest.skipUnless(dynamodb_support.AVAILABLE, dynamodb_support.SKIP_REASON)
class DynamoDBTestCase(unittest.TestCase):
    def setUp(self):
        self.local = dynamodb_support.LocalDynamoDB().start()
        self.addCleanup(self.local.stop)
        self.client = InterceptingClient(self.local.client)
        self.store = self.make_store()

    def make_store(self, **options):
        options.setdefault("backoff_base_seconds", 0)
        options.setdefault("backoff_cap_seconds", 0)
        return DynamoDBRevisionStore(TABLE_NAME, client=self.client, **options)

    def load(self, observation_date="2008-08-01"):
        return self.store.load_history(
            series_id="PAYEMS",
            observation_date=observation_date,
        )

    def lookup(self, as_of_date, observation_date="2008-08-01"):
        return self.store.lookup_value_as_of(
            series_id="PAYEMS",
            observation_date=observation_date,
            as_of_date=as_of_date,
        )

    def active_generation_key(self, observation_date="2008-08-01"):
        (manifest,) = [
            item
            for item in self.local.manifests()
            if item["sk"]["S"] == f"OBS#{observation_date}"
        ]
        generation = manifest["active_generation"]["S"]
        return f"SERIES#PAYEMS#OBS#{observation_date}#GEN#{generation}"

    def cleanup_record(self, generation_key):
        (record,) = [
            item
            for item in self.local.cleanup_records()
            if item["generation_key"]["S"] == generation_key
        ]
        return record

    def lifecycle(self, generation_key):
        return self.cleanup_record(generation_key)["lifecycle"]["S"]


class PublicationTests(DynamoDBTestCase):
    def test_first_publication_stores_manifest_generation_and_cleanup_record(self):
        self.store.save_history(history())

        (manifest,) = self.local.manifests()
        generation = manifest["active_generation"]["S"]
        generation_key = f"SERIES#PAYEMS#OBS#2008-08-01#GEN#{generation}"
        self.assertEqual(manifest["pk"], {"S": "SERIES#PAYEMS"})
        self.assertEqual(manifest["sk"], {"S": "OBS#2008-08-01"})
        self.assertEqual(manifest["vintage_count"], {"N": "2"})
        self.assertEqual(
            sorted(
                (item["pk"]["S"], item["sk"]["S"], float(item["value"]["N"]))
                for item in self.local.vintages()
            ),
            [
                (generation_key, "VINTAGE#2008-09-05", 137473.0),
                (generation_key, "VINTAGE#2008-10-03", 137477.0),
            ],
        )
        (cleanup,) = self.local.cleanup_records()
        self.assertEqual(cleanup["pk"], {"S": "SERIES#PAYEMS"})
        self.assertEqual(cleanup["sk"], {"S": f"RETIRE#2008-08-01#{generation}"})
        self.assertEqual(cleanup["generation_key"], {"S": generation_key})
        self.assertEqual(cleanup["lifecycle"], {"S": "active"})
        self.assertNotIn("delete_after", cleanup)
        self.assertEqual(len(self.local.items()), 4)

    def test_cleanup_record_is_written_before_any_vintage_and_manifest_last(self):
        self.store.save_history(long_history(30))

        self.assertEqual(
            self.client.operations(),
            [
                "get_item",
                "put_item",
                "batch_write_item",
                "batch_write_item",
                "transact_write_items",
            ],
        )
        (cleanup,) = self.client.parameters("put_item")
        self.assertTrue(cleanup["Item"]["sk"]["S"].startswith("RETIRE#2008-08-01#"))

    def test_manifest_is_absent_until_the_whole_generation_is_staged(self):
        seen = []
        self.client.before(
            "transact_write_items",
            lambda parameters: seen.append(
                (len(self.local.manifests()), len(self.local.vintages()))
            ),
        )

        self.store.save_history(long_history(60))

        self.assertEqual(seen, [(0, 60)])

    def test_replacement_moves_the_pointer_and_keeps_the_old_generation(self):
        self.store.save_history(history())
        old_key = self.active_generation_key()
        (staged_record,) = self.local.cleanup_records()

        self.store.save_history(REPLACEMENT)

        new_key = self.active_generation_key()
        self.assertNotEqual(new_key, old_key)
        self.assertEqual(len(self.local.manifests()), 1)
        self.assertEqual(len(self.local.vintages(old_key)), 2)
        self.assertEqual(len(self.local.vintages(new_key)), 1)
        records = {item["generation_key"]["S"]: item for item in self.local.cleanup_records()}
        self.assertEqual(set(records), {old_key, new_key})
        self.assertEqual(records[old_key]["sk"], staged_record["sk"])
        self.assertEqual(self.load(), REPLACEMENT)

    def test_replaced_generation_is_retired_and_due_after_the_grace_period(self):
        replaced = datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)
        self.store.save_history(history())
        old_key = self.active_generation_key()

        with patch.object(dynamodb, "_utc_now", return_value=replaced):
            self.store.save_history(REPLACEMENT)

        retired = self.cleanup_record(old_key)
        self.assertEqual(retired["lifecycle"], {"S": "retired"})
        self.assertEqual(retired["delete_after"], {"S": "2026-01-01T17:30:00Z"})
        active = self.cleanup_record(self.active_generation_key())
        self.assertEqual(active["lifecycle"], {"S": "active"})
        self.assertNotIn("delete_after", active)

    def test_staged_generation_is_due_after_the_grace_period(self):
        staged = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        def fail(parameters):
            raise client_error("InternalServerError", "BatchWriteItem")

        self.client.before("batch_write_item", fail)

        with patch.object(dynamodb, "_utc_now", return_value=staged):
            with self.assertRaises(DynamoDBRequestError):
                self.store.save_history(history())

        (record,) = self.local.cleanup_records()
        self.assertEqual(record["lifecycle"], {"S": "staged"})
        self.assertEqual(record["delete_after"], {"S": "2026-01-01T12:30:00Z"})

    def test_saving_identical_content_again_only_reads(self):
        self.store.save_history(history())
        before = self.local.items()
        self.client.calls.clear()

        saved = self.store.save_history(history())

        self.assertEqual(saved, 2)
        self.assertEqual(self.client.operations(), ["get_item", "query"])
        self.assertEqual(self.local.items(), before)

    def test_changed_value_on_the_same_dates_is_a_new_generation(self):
        self.store.save_history(history())
        old_key = self.active_generation_key()
        changed = history(
            revisions=(
                VintageValue("2008-09-05", 137473.0),
                VintageValue("2008-10-03", 137477.5),
            ),
        )

        self.store.save_history(changed)

        self.assertNotEqual(self.active_generation_key(), old_key)
        self.assertEqual(self.load(), changed)

    def test_store_only_uses_item_and_query_operations(self):
        self.store.save_histories((history(), history(observation_date="2008-09-01")))
        self.store.save_history(REPLACEMENT)
        self.load()
        self.lookup("2008-09-12")
        self.store.list_observations(series_id="PAYEMS")
        self.store.is_ready()

        self.assertEqual(
            set(self.client.operations()),
            {"get_item", "put_item", "batch_write_item", "transact_write_items", "query"},
        )

    def test_reads_that_follow_the_pointer_are_strongly_consistent(self):
        self.store.save_history(history())
        self.client.calls.clear()

        self.load()
        self.lookup("2008-09-12")
        self.store.list_observations(series_id="PAYEMS")
        self.store.save_history(REPLACEMENT)

        reads = self.client.parameters("get_item") + self.client.parameters("query")
        self.assertEqual(len(reads), 6)
        for parameters in reads:
            self.assertIs(parameters["ConsistentRead"], True)

    def test_lookup_reads_one_item_from_the_newest_matching_vintage(self):
        self.store.save_history(long_history(40))
        self.client.calls.clear()

        lookup = self.lookup("2008-09-20")

        self.assertEqual(lookup.revision, VintageValue("2008-09-20", 1015.0))
        self.assertEqual(self.client.operations(), ["get_item", "query"])
        (query,) = self.client.parameters("query")
        self.assertEqual(query["Limit"], 1)
        self.assertIs(query["ScanIndexForward"], False)


class BatchAndPaginationTests(DynamoDBTestCase):
    def test_staged_writes_are_split_into_batches_of_twenty_five(self):
        expected = long_history(60)

        saved = self.store.save_history(expected)

        sizes = [
            len(parameters["RequestItems"][TABLE_NAME])
            for parameters in self.client.parameters("batch_write_item")
        ]
        self.assertEqual(sizes, [25, 25, 10])
        self.assertEqual(saved, 60)
        self.assertEqual(self.load(), expected)
        self.assertEqual(len(self.local.vintages()), 60)

    def test_history_is_read_across_several_query_pages(self):
        expected = long_history(60)
        self.store.save_history(expected)
        self.client.page_limit = 7
        self.client.calls.clear()

        actual = self.load()

        queries = self.client.parameters("query")
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(len(queries), 9)
        self.assertNotIn("ExclusiveStartKey", queries[0])
        self.assertTrue(all("ExclusiveStartKey" in query for query in queries[1:]))

    def test_observations_are_listed_across_several_query_pages(self):
        dates = tuple(f"20{year:02d}-{month:02d}-01" for year in (7, 8) for month in range(1, 13))
        self.store.save_histories(
            history(observation_date=day, revisions=(VintageValue("2010-01-01", 1.0),))
            for day in reversed(dates)
        )
        self.client.page_limit = 5
        self.client.calls.clear()

        listed = self.store.list_observations(series_id="PAYEMS")

        self.assertEqual(listed, dates)
        self.assertGreaterEqual(len(self.client.parameters("query")), 5)

    def test_listing_ignores_cleanup_records_in_the_series_partition(self):
        self.store.save_history(history())
        self.store.save_history(REPLACEMENT)
        self.client.page_limit = 1

        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ("2008-08-01",))

    def test_lookup_is_correct_when_other_queries_are_paged(self):
        expected = long_history(60)
        self.store.save_history(expected)
        self.client.page_limit = 3

        for as_of_date in ("2008-09-04", "2008-09-05", "2008-10-15", "2030-01-01"):
            with self.subTest(as_of_date=as_of_date):
                self.assertEqual(
                    self.lookup(as_of_date).revision,
                    expected.value_as_of(as_of_date),
                )

    def test_history_longer_than_the_alfred_response_limit_is_stored(self):
        expected = long_history(2500)

        saved = self.store.save_history(expected)

        self.assertEqual(saved, 2500)
        self.assertEqual(self.load(), expected)
        self.assertEqual(self.lookup("9999-12-31").revision, expected.current)


class RetryTests(DynamoDBTestCase):
    def test_unprocessed_items_are_sent_again_until_stored(self):
        expected = long_history(60)
        self.client.process_at_most = 10

        self.store.save_history(expected)

        sizes = [
            len(parameters["RequestItems"][TABLE_NAME])
            for parameters in self.client.parameters("batch_write_item")
        ]
        self.assertEqual(sizes, [25, 15, 5, 25, 15, 5, 10])
        self.assertEqual(self.load(), expected)

    def test_only_the_unprocessed_items_are_resent(self):
        self.client.process_at_most = 1

        self.store.save_history(history())

        first, second = self.client.parameters("batch_write_item")
        resent = [
            request["PutRequest"]["Item"]["sk"]["S"]
            for request in second["RequestItems"][TABLE_NAME]
        ]
        self.assertEqual(resent, ["VINTAGE#2008-10-03"])
        self.assertEqual(len(first["RequestItems"][TABLE_NAME]), 2)

    def test_retries_stop_after_the_configured_attempts(self):
        self.store.save_history(history())
        store = self.make_store(max_attempts=3)
        self.client.process_at_most = 0
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBRetryExhaustedError, "3 attempts"):
            store.save_history(REPLACEMENT)

        self.assertEqual(len(self.client.parameters("batch_write_item")), 3)
        self.assertNotIn("transact_write_items", self.client.operations())
        self.assertEqual(self.load(), history())

    def test_backoff_is_exponential_jittered_and_capped(self):
        store = self.make_store(
            max_attempts=6,
            backoff_base_seconds=0.5,
            backoff_cap_seconds=3.0,
        )
        self.client.process_at_most = 0
        ceilings = []

        def choose(low, high):
            ceilings.append((low, high))
            return high / 2

        with (
            patch.object(dynamodb.random, "uniform", side_effect=choose),
            patch.object(dynamodb.time, "sleep") as sleep,
        ):
            with self.assertRaises(DynamoDBRetryExhaustedError):
                store.save_history(history())

        self.assertEqual(ceilings, [(0, 0.5), (0, 1.0), (0, 2.0), (0, 3.0), (0, 3.0)])
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.25, 0.5, 1.0, 1.5, 1.5],
        )

    def test_unprocessed_items_under_another_table_name_stop_the_save(self):
        self.client.reply_once(
            "batch_write_item",
            {"UnprocessedItems": {"another-table": [{"PutRequest": {"Item": {}}}]}},
        )

        with self.assertRaisesRegex(DynamoDBStoreError, "unexpected table"):
            self.store.save_history(history())

        self.assertEqual(self.local.manifests(), [])

    def test_empty_unprocessed_entry_counts_as_complete(self):
        target = self.local.client.batch_write_item

        def store_then_report_nothing_left(parameters):
            target(**parameters)
            self.client.reply_once("batch_write_item", {"UnprocessedItems": {TABLE_NAME: []}})

        self.client.before("batch_write_item", store_then_report_nothing_left)

        self.store.save_history(history())

        self.assertEqual(self.load(), history())

    def test_transient_transaction_cancellation_is_retried(self):
        self.store.save_history(history())
        failures = [client_error("TransactionCanceledException", "TransactWriteItems")]

        def cancel_once(parameters):
            if failures:
                raise failures.pop()

        self.client.before("transact_write_items", cancel_once)

        self.store.save_history(REPLACEMENT)

        self.assertEqual(self.load(), REPLACEMENT)

    def test_repeated_transaction_cancellation_is_bounded(self):
        self.store.save_history(history())
        store = self.make_store(max_attempts=4)

        def cancel(parameters):
            raise client_error("TransactionCanceledException", "TransactWriteItems")

        self.client.before("transact_write_items", cancel)
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBRetryExhaustedError, "4 attempts"):
            store.save_history(REPLACEMENT)

        self.assertEqual(len(self.client.parameters("transact_write_items")), 4)
        self.assertEqual(self.load(), history())


class ConcurrencyTests(DynamoDBTestCase):
    def publish_while_staged(self, other_history):
        """Let a second writer publish after this store has staged its generation."""

        rival = DynamoDBRevisionStore(TABLE_NAME, client=self.local.client)
        pending = [other_history]

        def interleave(parameters):
            if pending:
                rival.save_history(pending.pop())

        self.client.before("transact_write_items", interleave)
        self.client.calls.clear()

    def test_losing_writer_does_not_overwrite_the_winner(self):
        winner = history(revisions=(VintageValue("2008-09-05", 1.0),))
        self.store.save_history(history())
        self.publish_while_staged(winner)

        with self.assertRaisesRegex(DynamoDBConcurrencyError, "PAYEMS 2008-08-01"):
            self.store.save_history(REPLACEMENT)

        self.assertEqual(self.load(), winner)
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)

    def test_losing_generation_stays_tracked_for_cleanup(self):
        winner = history(revisions=(VintageValue("2008-09-05", 1.0),))
        self.store.save_history(history())
        self.publish_while_staged(winner)

        with self.assertRaises(DynamoDBConcurrencyError):
            self.store.save_history(REPLACEMENT)

        active = self.active_generation_key()
        tracked = {item["generation_key"]["S"] for item in self.local.cleanup_records()}
        staged = {item["pk"]["S"] for item in self.local.vintages()}
        self.assertEqual(len(staged), 3)
        self.assertEqual(tracked, staged)
        self.assertIn(active, tracked)

    def test_losing_writer_succeeds_when_the_winner_published_the_same_history(self):
        self.store.save_history(history())
        self.publish_while_staged(REPLACEMENT)

        saved = self.store.save_history(REPLACEMENT)

        self.assertEqual(saved, 1)
        self.assertEqual(self.load(), REPLACEMENT)
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)
        self.assertEqual(len(self.local.manifests()), 1)

    def test_first_publication_race_has_one_winner(self):
        winner = history(revisions=(VintageValue("2008-09-05", 1.0),))
        self.publish_while_staged(winner)

        with self.assertRaises(DynamoDBConcurrencyError):
            self.store.save_history(history())

        self.assertEqual(self.load(), winner)
        self.assertEqual(len(self.local.manifests()), 1)

    def test_publication_is_conditional_on_the_generation_that_was_read(self):
        self.store.save_history(history())
        expected = self.active_generation_key().rsplit("#", 1)[1]
        self.client.calls.clear()

        self.store.save_history(REPLACEMENT)

        (transaction,) = self.client.parameters("transact_write_items")
        manifest = transaction["TransactItems"][0]["Put"]
        self.assertEqual(manifest["ConditionExpression"], "active_generation = :expected")
        self.assertEqual(manifest["ExpressionAttributeValues"], {":expected": {"S": expected}})

    def test_successful_publication_reported_as_cancelled_is_not_repeated(self):
        # A lost response makes the SDK resend a transaction that already
        # committed, and the condition then fails against the new pointer.
        self.store.save_history(history())
        target = self.local.client.transact_write_items
        pending = [True]

        def commit_then_fail(parameters):
            if pending:
                pending.pop()
                target(**parameters)
                raise client_error("TransactionCanceledException", "TransactWriteItems")

        self.client.before("transact_write_items", commit_then_fail)
        self.client.calls.clear()

        self.store.save_history(REPLACEMENT)

        self.assertEqual(self.load(), REPLACEMENT)
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)


LONG_AFTER = datetime(2100, 1, 1, tzinfo=timezone.utc)


class CleanupClaimTests(DynamoDBTestCase):
    """Publication and a cleanup claim must never both succeed for a generation."""

    def claim(self, generation_key, now=LONG_AFTER, store=None):
        return (store or self.store).claim_generation_for_cleanup(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            generation=generation_key.rsplit("#", 1)[1],
            now=now,
        )

    def claim_while_staged(self):
        """Have a cleanup worker claim the new generation just before it is published."""

        worker = DynamoDBRevisionStore(TABLE_NAME, client=self.local.client)
        outcome = {}

        def interleave(parameters):
            if not outcome:
                staged_key = parameters["TransactItems"][1]["Update"]["Key"]
                (record,) = [
                    item
                    for item in self.local.cleanup_records()
                    if item["sk"] == staged_key["sk"]
                ]
                outcome["generation_key"] = record["generation_key"]["S"]
                outcome["claimed"] = self.claim(outcome["generation_key"], store=worker)

        self.client.before("transact_write_items", interleave)
        return outcome

    def test_claim_before_publication_stops_the_publication(self):
        self.store.save_history(history())
        old_key = self.active_generation_key()
        outcome = self.claim_while_staged()
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBStoreError, "claimed for cleanup"):
            self.store.save_history(REPLACEMENT)

        self.assertTrue(outcome["claimed"])
        self.assertEqual(self.active_generation_key(), old_key)
        self.assertEqual(self.load(), history())
        self.assertEqual(self.lifecycle(old_key), "active")
        self.assertEqual(self.lifecycle(outcome["generation_key"]), "deleting")
        self.assertEqual(len(self.client.parameters("transact_write_items")), 1)

    def test_claim_before_a_first_publication_leaves_no_observation(self):
        outcome = self.claim_while_staged()

        with self.assertRaisesRegex(DynamoDBStoreError, "claimed for cleanup"):
            self.store.save_history(history())

        self.assertTrue(outcome["claimed"])
        self.assertEqual(self.local.manifests(), [])
        self.assertIsNone(self.load())

    def test_publication_before_claim_stops_the_claim(self):
        self.store.save_history(history())
        active_key = self.active_generation_key()

        claimed = self.claim(active_key)

        self.assertFalse(claimed)
        self.assertEqual(self.lifecycle(active_key), "active")
        self.assertEqual(self.load(), history())

    def test_claim_cannot_take_a_generation_the_manifest_points_at(self):
        # Even if the cleanup record wrongly says "retired", the manifest decides.
        self.store.save_history(history())
        active_key = self.active_generation_key()
        record = self.cleanup_record(active_key)
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={
                **record,
                "lifecycle": {"S": "retired"},
                "delete_after": {"S": "2000-01-01T00:00:00Z"},
            },
        )

        self.assertFalse(self.claim(active_key))
        self.assertEqual(self.lifecycle(active_key), "retired")
        self.assertEqual(self.load(), history())

    def test_retired_generation_can_be_claimed_once_after_its_deadline(self):
        published = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.store.save_history(history())
        old_key = self.active_generation_key()
        with patch.object(dynamodb, "_utc_now", return_value=published):
            self.store.save_history(REPLACEMENT)

        too_early = self.claim(old_key, now=published + timedelta(minutes=29, seconds=59))
        on_time = self.claim(old_key, now=published + timedelta(minutes=30))
        again = self.claim(old_key, now=published + timedelta(hours=1))

        self.assertEqual((too_early, on_time, again), (False, True, False))
        self.assertEqual(self.lifecycle(old_key), "deleting")
        self.assertEqual(self.lifecycle(self.active_generation_key()), "active")
        self.assertEqual(len(self.local.vintages(old_key)), 2)
        self.assertEqual(self.load(), REPLACEMENT)

    def test_abandoned_staged_generation_can_be_claimed_after_its_deadline(self):
        staged = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        def fail(parameters):
            raise client_error("InternalServerError", "BatchWriteItem")

        self.client.before("batch_write_item", fail)
        with patch.object(dynamodb, "_utc_now", return_value=staged):
            with self.assertRaises(DynamoDBRequestError):
                self.store.save_history(history())
        (record,) = self.local.cleanup_records()
        abandoned_key = record["generation_key"]["S"]

        too_early = self.claim(abandoned_key, now=staged + timedelta(minutes=10))
        on_time = self.claim(abandoned_key, now=staged + timedelta(minutes=30))

        self.assertEqual((too_early, on_time), (False, True))

    def test_claim_defaults_to_the_current_time(self):
        self.store.save_history(history())
        old_key = self.active_generation_key()
        self.store.save_history(REPLACEMENT)

        self.assertFalse(self.claim(old_key, now=None))

    def test_unknown_generation_cannot_be_claimed(self):
        self.store.save_history(history())

        claimed = self.claim("SERIES#PAYEMS#OBS#2008-08-01#GEN#" + "0" * 32)

        self.assertFalse(claimed)
        self.assertEqual(len(self.local.cleanup_records()), 1)

    def test_claim_validates_its_arguments_and_respects_read_only(self):
        with self.assertRaises(ValueError):
            self.claim("SERIES#PAYEMS#OBS#2008-08-01#GEN#not-a-generation")
        with self.assertRaisesRegex(DynamoDBStoreError, "read-only"):
            self.claim("x#" + "0" * 32, store=self.make_store(read_only=True))

        self.assertEqual(self.client.calls, [])

    def test_claim_errors_other_than_cancellation_are_raised(self):
        def fail(parameters):
            raise client_error("AccessDeniedException", "TransactWriteItems")

        self.client.before("transact_write_items", fail)

        with self.assertRaises(DynamoDBRequestError):
            self.claim("x#" + "0" * 32)


class RepairTests(DynamoDBTestCase):
    """Saving a history again must notice when the stored generation is damaged."""

    def setUp(self):
        super().setUp()
        self.expected = long_history(30)
        self.store.save_history(self.expected)
        self.damaged_key = self.active_generation_key()
        self.manifest = self.local.manifests()

    def put_vintage(self, vintage_date, **attributes):
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={
                "pk": {"S": self.damaged_key},
                "sk": {"S": f"VINTAGE#{vintage_date}"},
                **attributes,
            },
        )

    def assert_resave_repairs(self):
        self.assertEqual(self.local.manifests(), self.manifest)

        saved = self.store.save_history(self.expected)

        self.assertEqual(saved, 30)
        self.assertNotEqual(self.active_generation_key(), self.damaged_key)
        self.assertEqual(self.load(), self.expected)
        self.assertEqual(self.lookup("9999-12-31").revision, self.expected.current)
        self.assertEqual(self.lifecycle(self.damaged_key), "retired")

    def test_missing_item_is_repaired(self):
        self.local.client.delete_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": self.damaged_key}, "sk": {"S": "VINTAGE#2008-09-20"}},
        )

        self.assert_resave_repairs()

    def test_modified_value_with_the_same_count_is_repaired(self):
        self.put_vintage("2008-09-20", value={"N": "999999"})

        self.assert_resave_repairs()

    def test_modified_last_value_is_repaired(self):
        self.put_vintage(self.expected.current.vintage_date, value={"N": "-1"})

        self.assert_resave_repairs()

    def test_item_replaced_by_one_on_another_date_is_repaired(self):
        last = self.expected.current
        self.local.client.delete_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": self.damaged_key}, "sk": {"S": f"VINTAGE#{last.vintage_date}"}},
        )
        self.put_vintage("2030-01-01", value={"N": str(last.value)})

        self.assert_resave_repairs()

    def test_extra_item_is_repaired(self):
        self.put_vintage("2030-01-01", value={"N": "1"})

        self.assert_resave_repairs()

    def test_malformed_item_is_repaired(self):
        self.put_vintage("2008-09-20", value={"S": "1015.0"})

        self.assert_resave_repairs()

    def test_damage_is_found_on_a_later_query_page(self):
        self.client.page_limit = 4
        self.put_vintage(self.expected.current.vintage_date, value={"N": "-1"})

        self.assert_resave_repairs()

    def test_intact_generation_is_left_alone_when_numbers_are_formatted_differently(self):
        # DynamoDB may return a number in another form than the one written.
        self.put_vintage("2008-09-20", value={"N": "1.015E+3"})
        self.client.calls.clear()

        self.store.save_history(self.expected)

        self.assertEqual(self.active_generation_key(), self.damaged_key)
        self.assertEqual(set(self.client.operations()), {"get_item", "query"})


class PartialFailureTests(DynamoDBTestCase):
    def fail_batch_write(self, call_number):
        seen = []

        def fail(parameters):
            seen.append(parameters)
            if len(seen) == call_number:
                raise client_error("InternalServerError", "BatchWriteItem")

        self.client.before("batch_write_item", fail)

    def test_failure_while_staging_leaves_the_old_generation_readable(self):
        original = long_history(40)
        self.store.save_history(original)
        old_key = self.active_generation_key()
        self.client.calls.clear()
        self.fail_batch_write(2)

        with self.assertRaises(DynamoDBRequestError):
            self.store.save_history(long_history(60))

        self.assertEqual(self.active_generation_key(), old_key)
        self.assertEqual(self.load(), original)
        self.assertEqual(self.lookup("9999-12-31").revision, original.current)
        abandoned = {item["pk"]["S"] for item in self.local.vintages()} - {old_key}
        (abandoned_key,) = abandoned
        self.assertEqual(len(self.local.vintages(abandoned_key)), 25)
        self.assertIn(
            abandoned_key,
            {item["generation_key"]["S"] for item in self.local.cleanup_records()},
        )

    def test_failure_while_staging_a_first_publication_leaves_no_observation(self):
        self.fail_batch_write(2)

        with self.assertRaises(DynamoDBRequestError):
            self.store.save_history(long_history(60))

        self.assertIsNone(self.load())
        self.assertFalse(self.lookup("9999-12-31").observation_exists)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ())
        self.assertEqual(len(self.local.vintages()), 25)
        self.assertEqual(len(self.local.cleanup_records()), 1)

    def test_failure_while_publishing_leaves_the_old_generation_readable(self):
        self.store.save_history(history())

        def fail(parameters):
            raise client_error("InternalServerError", "TransactWriteItems")

        self.client.before("transact_write_items", fail)

        with self.assertRaises(DynamoDBRequestError):
            self.store.save_history(REPLACEMENT)

        self.assertEqual(self.load(), history())

    def test_batch_failure_keeps_observations_already_published(self):
        first = history(observation_date="2008-08-01")
        second = history(observation_date="2008-09-01")
        self.fail_batch_write(2)

        with self.assertRaises(DynamoDBRequestError):
            self.store.save_histories((first, second))

        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ("2008-08-01",))
        self.assertEqual(self.load("2008-08-01"), first)
        self.assertIsNone(self.load("2008-09-01"))

    def test_interrupted_save_can_be_repeated(self):
        expected = long_history(60)
        self.fail_batch_write(3)
        with self.assertRaises(DynamoDBRequestError):
            self.store.save_history(expected)

        self.store.save_history(expected)

        self.assertEqual(self.load(), expected)


class NumericTests(DynamoDBTestCase):
    def test_values_are_stored_as_dynamodb_numbers(self):
        self.store.save_history(
            history(
                revisions=(
                    VintageValue("2008-09-05", 0.1),
                    VintageValue("2008-10-03", 1e20),
                    VintageValue("2008-11-07", -0.0),
                ),
            )
        )

        stored = {
            item["sk"]["S"]: item["value"]
            for item in self.local.vintages()
        }
        self.assertEqual(set(stored["VINTAGE#2008-09-05"]), {"N"})
        self.assertEqual(Decimal(stored["VINTAGE#2008-09-05"]["N"]), Decimal("0.1"))
        self.assertEqual(Decimal(stored["VINTAGE#2008-10-03"]["N"]), Decimal("1E+20"))
        self.assertEqual(stored["VINTAGE#2008-11-07"], {"N": "0"})

    def test_number_strings_round_trip_every_float_exactly(self):
        for value in (
            0.1,
            1 / 3,
            -137473.25,
            5e-130,
            9.9e125,
            2.2250738585072014e-100,
            123456789012345.67,
            float(2**63),
        ):
            with self.subTest(value=value):
                text = dynamodb._to_number(value)
                self.assertEqual(float(text), value)
                self.assertLessEqual(len(Decimal(text).as_tuple().digits), 38)

    def test_integer_values_are_accepted(self):
        self.store.save_history(history(revisions=(VintageValue("2008-09-05", 5),)))

        value = self.load().current.value
        self.assertEqual(value, 5.0)
        self.assertIs(type(value), float)

    def test_integers_a_float_holds_exactly_round_trip_unchanged(self):
        for value in (0, -1, 2**53, -(2**53), 2**53 + 2, -(2**53 + 2), 2**62, 2**400):
            with self.subTest(value=value):
                self.store.save_history(history(revisions=(VintageValue("2008-09-05", value),)))

                stored = self.load().current.value

                self.assertEqual(stored, value)
                self.assertEqual(int(stored), value)

    def test_integers_a_float_would_change_are_rejected(self):
        self.store.save_history(history())
        self.client.calls.clear()
        for value in (2**53 + 1, -(2**53 + 1), 2**63 - 1, 10**23, 10**125, -(10**125)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(DynamoDBValueError, "without changing it"):
                    self.store.save_history(history(revisions=(VintageValue("2008-09-05", value),)))

        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.load(), history())

    def test_integers_too_large_for_a_float_are_rejected(self):
        with self.assertRaisesRegex(DynamoDBValueError, "range"):
            dynamodb._to_number(10**400)
        with self.assertRaisesRegex(DynamoDBValueError, "range"):
            dynamodb._to_number(-(10**400))

    def test_booleans_are_not_numbers(self):
        with self.assertRaisesRegex(DynamoDBValueError, "must be a number"):
            dynamodb._to_number(True)

    def test_dynamodb_range_limits(self):
        largest = math.nextafter(1e126, 0)
        smallest = 1e-130
        for value in (largest, -largest, smallest, -smallest):
            with self.subTest(accepted=value):
                self.assertEqual(float(dynamodb._to_number(value)), value)
        for value in (
            1e126,
            -1e126,
            math.nextafter(smallest, 0),
            -math.nextafter(smallest, 0),
            2**419,
            -(2**419),
        ):
            with self.subTest(rejected=value):
                with self.assertRaisesRegex(DynamoDBValueError, "range"):
                    dynamodb._to_number(value)

    def test_values_outside_the_dynamodb_range_are_rejected_before_writing(self):
        storable = history(observation_date="2008-07-01")
        for value in (1.7976931348623157e308, 1e126, 5e-324, -1e-131):
            with self.subTest(value=value):
                unstorable = history(revisions=(VintageValue("2008-09-05", value),))

                with self.assertRaisesRegex(DynamoDBValueError, "range"):
                    self.store.save_histories((storable, unstorable))

        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.local.items(), [])

    def test_range_errors_are_value_errors(self):
        with self.assertRaises(ValueError):
            self.store.save_history(history(revisions=(VintageValue("2008-09-05", 1e300),)))

    def test_numbers_stored_with_more_precision_are_read_as_floats(self):
        self.store.save_history(history(revisions=(VintageValue("2008-09-05", 1.0),)))
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={
                "pk": {"S": self.active_generation_key()},
                "sk": {"S": "VINTAGE#2008-09-05"},
                "value": {"N": "0.12345678901234567890123456789012345678"},
            },
        )

        value = self.load().current.value

        self.assertIs(type(value), float)
        self.assertEqual(value, 0.12345678901234568)


class MalformedDataTests(DynamoDBTestCase):
    def setUp(self):
        super().setUp()
        self.store.save_history(history())
        self.generation_key = self.active_generation_key()

    def overwrite_vintage(self, sort_key="VINTAGE#2008-09-05", **attributes):
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={"pk": {"S": self.generation_key}, "sk": {"S": sort_key}, **attributes},
        )

    def overwrite_manifest(self, **attributes):
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={"pk": {"S": "SERIES#PAYEMS"}, "sk": {"S": "OBS#2008-08-01"}, **attributes},
        )

    def assert_reads_fail(self):
        with self.assertRaises(DynamoDBDataError):
            self.load()
        with self.assertRaises(DynamoDBDataError):
            self.lookup("2008-09-12")

    def test_manifest_without_an_active_generation(self):
        self.overwrite_manifest(vintage_count={"N": "2"})

        self.assert_reads_fail()
        with self.assertRaises(DynamoDBDataError):
            self.store.save_history(REPLACEMENT)

    def test_manifest_with_a_non_string_generation(self):
        self.overwrite_manifest(active_generation={"N": "7"}, vintage_count={"N": "2"})

        self.assert_reads_fail()

    def test_manifest_with_an_invalid_vintage_count(self):
        for count in ({"S": "2"}, {"N": "0"}, {"N": "1.5"}):
            with self.subTest(count=count):
                self.overwrite_manifest(active_generation={"S": "abc"}, vintage_count=count)

                self.assert_reads_fail()

    def test_vintage_without_a_value(self):
        self.overwrite_vintage()

        self.assert_reads_fail()

    def test_vintage_with_a_non_numeric_value(self):
        self.overwrite_vintage(value={"S": "137473.0"})

        self.assert_reads_fail()

    def test_vintage_with_an_invalid_date(self):
        self.overwrite_vintage(sort_key="VINTAGE#2008-9-5", value={"N": "1"})

        with self.assertRaises(DynamoDBDataError):
            self.load()
        with self.assertRaises(DynamoDBDataError):
            self.lookup("9999-12-31")

    def test_generation_shorter_than_its_manifest(self):
        self.local.client.delete_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": self.generation_key}, "sk": {"S": "VINTAGE#2008-10-03"}},
        )

        with self.assertRaisesRegex(DynamoDBDataError, "does not match its manifest"):
            self.load()

    def test_empty_generation_is_not_reported_as_unpublished(self):
        for item in self.local.vintages(self.generation_key):
            self.local.client.delete_item(
                TableName=TABLE_NAME,
                Key={"pk": item["pk"], "sk": item["sk"]},
            )

        for as_of_date in ("2008-09-04", "2008-09-12"):
            with self.subTest(as_of_date=as_of_date):
                with self.assertRaises(DynamoDBDataError):
                    self.lookup(as_of_date)

    def test_manifest_with_an_invalid_observation_date(self):
        self.local.client.put_item(
            TableName=TABLE_NAME,
            Item={"pk": {"S": "SERIES#PAYEMS"}, "sk": {"S": "OBS#2008-8-1"}},
        )

        with self.assertRaises(DynamoDBDataError):
            self.store.list_observations(series_id="PAYEMS")

    def test_errors_do_not_repeat_stored_content(self):
        self.overwrite_vintage(value={"S": "private-payload"})

        with self.assertRaises(DynamoDBDataError) as raised:
            self.load()

        self.assertNotIn("private-payload", str(raised.exception))


class ConfigurationTests(DynamoDBTestCase):
    def test_invalid_table_names_are_rejected(self):
        for table_name in ("", "ab", "asof test", "arn:aws:dynamodb:us-east-1:1:table/x", None):
            with self.subTest(table_name=table_name):
                with self.assertRaisesRegex(DynamoDBConfigurationError, "table_name"):
                    DynamoDBRevisionStore(table_name, client=self.client)

    def test_invalid_retry_settings_are_rejected(self):
        for options in (
            {"max_attempts": 0},
            {"max_attempts": 2.5},
            {"backoff_base_seconds": -1},
            {"backoff_base_seconds": 2, "backoff_cap_seconds": 1},
        ):
            with self.subTest(options=options):
                with self.assertRaises(DynamoDBConfigurationError):
                    DynamoDBRevisionStore(TABLE_NAME, client=self.client, **options)

    def test_client_and_connection_settings_are_mutually_exclusive(self):
        with self.assertRaises(DynamoDBConfigurationError):
            DynamoDBRevisionStore(TABLE_NAME, client=self.client, region_name="us-east-2")

    def test_missing_optional_dependency_is_explained(self):
        with patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaisesRegex(DynamoDBConfigurationError, r"\[dynamodb\]"):
                DynamoDBRevisionStore(TABLE_NAME, region_name="us-east-1")

    def test_missing_region_is_a_configuration_error(self):
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in {"AWS_DEFAULT_REGION", "AWS_REGION", "AWS_PROFILE"}
        }
        environment["AWS_CONFIG_FILE"] = os.devnull
        environment["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(DynamoDBConfigurationError, "region") as raised:
                DynamoDBRevisionStore(TABLE_NAME)

        self.assertIsNone(raised.exception.__cause__)

    def test_invalid_endpoint_is_a_configuration_error_without_the_url(self):
        with self.assertRaises(DynamoDBConfigurationError) as raised:
            DynamoDBRevisionStore(
                TABLE_NAME,
                region_name="us-east-1",
                endpoint_url="not a url with secret-token",
            )

        self.assertNotIn("secret-token", str(raised.exception))

    def test_store_builds_its_own_client_from_region_and_table_name(self):
        self.store.save_history(history())

        with DynamoDBRevisionStore(TABLE_NAME, region_name=dynamodb_support.REGION) as store:
            loaded = store.load_history(series_id="PAYEMS", observation_date="2008-08-01")
            ready = store.is_ready()

        self.assertEqual(loaded, history())
        self.assertTrue(ready)

    def test_construction_makes_no_requests(self):
        DynamoDBRevisionStore(TABLE_NAME, client=self.client)

        self.assertEqual(self.client.calls, [])

    def test_missing_table_is_not_created_and_is_not_ready(self):
        store = DynamoDBRevisionStore("asof-absent", client=self.client)

        self.assertFalse(store.is_ready())
        with self.assertRaises(DynamoDBRequestError) as raised:
            store.load_history(series_id="PAYEMS", observation_date="2008-08-01")
        with self.assertRaises(DynamoDBRequestError):
            store.save_history(history())

        self.assertEqual(raised.exception.code, "ResourceNotFoundException")
        self.assertEqual(raised.exception.operation, "get_item")
        self.assertEqual(self.local.client.list_tables()["TableNames"], [TABLE_NAME])

    def test_table_with_another_key_schema_is_not_ready(self):
        self.local.client.create_table(
            TableName="asof-other-keys",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = DynamoDBRevisionStore("asof-other-keys", client=self.client)

        self.assertFalse(store.is_ready())


class ErrorAndLifecycleTests(DynamoDBTestCase):
    def test_request_errors_keep_only_the_operation_and_code(self):
        secret = "arn:aws:dynamodb:us-east-2:123456789012:table/asof AKIAEXAMPLE"

        def deny(parameters):
            raise client_error("AccessDeniedException", "GetItem", secret)

        self.client.before("get_item", deny)

        with self.assertRaises(DynamoDBRequestError) as raised:
            self.load()

        self.assertEqual(str(raised.exception), "DynamoDB get_item failed (AccessDeniedException)")
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertFalse(self.store.is_ready())

    def test_unusual_error_codes_are_not_echoed(self):
        def fail(parameters):
            raise client_error("code with spaces and <payload>", "GetItem")

        self.client.before("get_item", fail)

        with self.assertRaises(DynamoDBRequestError) as raised:
            self.load()

        self.assertEqual(raised.exception.code, "UnknownError")

    def test_connection_failures_do_not_expose_the_endpoint(self):
        from botocore.exceptions import EndpointConnectionError

        def fail(parameters):
            raise EndpointConnectionError(endpoint_url="https://internal.example:8000")

        self.client.before("query", fail)

        with self.assertRaises(DynamoDBRequestError) as raised:
            self.store.list_observations(series_id="PAYEMS")

        self.assertEqual(raised.exception.code, "EndpointConnectionError")
        self.assertNotIn("internal.example", str(raised.exception))

    def test_unrelated_exceptions_are_not_disguised(self):
        def fail(parameters):
            raise KeyError("bug")

        self.client.before("get_item", fail)

        with self.assertRaises(KeyError):
            self.load()

    def test_closed_store_rejects_reads_and_writes(self):
        self.store.save_history(history())
        self.store.close()
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBStoreError, "closed"):
            self.load()
        with self.assertRaisesRegex(DynamoDBStoreError, "closed"):
            self.store.save_history(REPLACEMENT)
        with self.assertRaisesRegex(DynamoDBStoreError, "closed"):
            self.store.save_histories(())

        self.assertEqual(self.client.calls, [])

    def test_closing_leaves_an_injected_client_usable(self):
        self.store.close()

        self.assertTrue(self.make_store().is_ready())

    def test_read_only_store_reads_but_does_not_write(self):
        self.store.save_history(history())
        reader = self.make_store(read_only=True)
        self.client.calls.clear()

        with self.assertRaisesRegex(DynamoDBStoreError, "read-only"):
            reader.save_history(REPLACEMENT)

        self.assertEqual(self.client.calls, [])
        self.assertEqual(
            reader.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
            history(),
        )
        self.assertTrue(reader.is_ready())

    def test_readers_need_only_get_item_and_query(self):
        self.store.save_history(history())
        self.client.calls.clear()

        self.load()
        self.lookup("2008-09-12")
        self.store.list_observations(series_id="PAYEMS")
        self.store.is_ready()

        self.assertEqual(set(self.client.operations()), {"get_item", "query"})

    def test_identifiers_that_could_reach_another_key_are_rejected(self):
        for series_id in ("PAYEMS#OBS#2008-08-01", "", "PAYEMS "):
            with self.subTest(series_id=series_id):
                with self.assertRaises(ValueError):
                    self.store.list_observations(series_id=series_id)
                with self.assertRaises(ValueError):
                    self.store.load_history(series_id=series_id, observation_date="2008-08-01")
        for bad_date in ("2008-8-1", "2008-08-01#GEN#x", "", None):
            with self.subTest(bad_date=bad_date):
                with self.assertRaises(ValueError):
                    self.store.load_history(series_id="PAYEMS", observation_date=bad_date)
                with self.assertRaises(ValueError):
                    self.store.lookup_value_as_of(
                        series_id="PAYEMS",
                        observation_date="2008-08-01",
                        as_of_date=bad_date,
                    )

        self.assertEqual(self.client.calls, [])

    def test_only_revision_histories_can_be_saved(self):
        with self.assertRaises(TypeError):
            self.store.save_histories(({"series_id": "PAYEMS"},))

        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
