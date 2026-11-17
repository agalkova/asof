"""Behavior every revision store must share, run against each local backend."""

import unittest
from unittest.mock import patch

import dynamodb_support
from asof import (
    DynamoDBRevisionStore,
    RevisionHistory,
    SQLiteRevisionStore,
    VintageValue,
)
from asof.sync import sync_revision_histories


def history(
    series_id="PAYEMS",
    observation_date="2008-08-01",
    revisions=(
        VintageValue("2008-09-05", 137473.0),
        VintageValue("2008-10-03", 137477.0),
        VintageValue("2009-02-06", 137211.0),
    ),
):
    return RevisionHistory(
        series_id=series_id,
        observation_date=observation_date,
        revisions=revisions,
    )


class RevisionStoreContract:
    """Mixed into one ``TestCase`` per backend; ``self.store`` is the subject."""

    def load(self, series_id="PAYEMS", observation_date="2008-08-01"):
        return self.store.load_history(
            series_id=series_id,
            observation_date=observation_date,
        )

    def lookup(self, as_of_date, series_id="PAYEMS", observation_date="2008-08-01"):
        return self.store.lookup_value_as_of(
            series_id=series_id,
            observation_date=observation_date,
            as_of_date=as_of_date,
        )

    def test_history_round_trip(self):
        expected = history()

        saved = self.store.save_history(expected)

        self.assertEqual(saved, 3)
        self.assertEqual(self.load(), expected)

    def test_shorter_history_replaces_every_earlier_vintage(self):
        replacement = history(revisions=(VintageValue("2008-09-05", 137400.0),))
        self.store.save_history(history())

        saved = self.store.save_history(replacement)

        self.assertEqual(saved, 1)
        self.assertEqual(self.load(), replacement)
        after_dropped_vintages = self.lookup("2030-01-01")
        self.assertEqual(after_dropped_vintages.revision, VintageValue("2008-09-05", 137400.0))

    def test_replacement_does_not_merge_disjoint_vintages(self):
        replacement = history(
            revisions=(
                VintageValue("2008-09-19", 1.0),
                VintageValue("2008-11-07", 2.0),
            ),
        )
        self.store.save_history(history())

        self.store.save_history(replacement)

        self.assertEqual(self.load(), replacement)
        self.assertIsNone(self.lookup("2008-09-12").revision)

    def test_saving_the_same_history_again_is_idempotent(self):
        expected = history()

        first = self.store.save_history(expected)
        second = self.store.save_history(expected)

        self.assertEqual((first, second), (3, 3))
        self.assertEqual(self.load(), expected)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ("2008-08-01",))

    def test_empty_input_stores_nothing(self):
        self.assertEqual(self.store.save_histories(()), 0)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ())

    def test_generator_input_is_consumed_once(self):
        dates = ("2008-08-01", "2008-09-01")

        saved = self.store.save_histories(history(observation_date=day) for day in dates)

        self.assertEqual(saved, 6)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), dates)

    def test_multiple_histories_are_saved_together(self):
        first = history(observation_date="2008-08-01")
        second = history(
            observation_date="2008-09-01",
            revisions=(VintageValue("2008-10-03", 136978.0),),
        )
        other_series = history(series_id="UNRATE", revisions=(VintageValue("2008-09-05", 6.1),))

        saved = self.store.save_histories((first, second, other_series))

        self.assertEqual(saved, 5)
        self.assertEqual(self.load(observation_date="2008-08-01"), first)
        self.assertEqual(self.load(observation_date="2008-09-01"), second)
        self.assertEqual(self.load(series_id="UNRATE"), other_series)

    def test_last_duplicate_observation_in_a_batch_wins(self):
        earlier = history()
        later = history(revisions=(VintageValue("2008-09-05", 1.5),))

        saved = self.store.save_histories((earlier, later))

        self.assertEqual(saved, 4)
        self.assertEqual(self.load(), later)
        self.assertEqual(self.store.list_observations(series_id="PAYEMS"), ("2008-08-01",))

    def test_observation_dates_are_listed_in_ascending_order(self):
        for day in ("2008-10-01", "2007-12-01", "2008-09-01", "2008-01-01"):
            self.store.save_history(history(observation_date=day))

        dates = self.store.list_observations(series_id="PAYEMS")

        self.assertEqual(dates, ("2007-12-01", "2008-01-01", "2008-09-01", "2008-10-01"))

    def test_revisions_are_loaded_in_ascending_order(self):
        # Chosen so that an ordering by value, or by day of month, would differ.
        expected = history(
            revisions=(
                VintageValue("2008-09-30", 9.0),
                VintageValue("2008-10-02", 3.0),
                VintageValue("2009-01-15", 7.0),
                VintageValue("2010-01-01", 1.0),
            ),
        )

        self.store.save_history(expected)

        loaded = self.load()
        self.assertEqual(
            [revision.vintage_date for revision in loaded.revisions],
            ["2008-09-30", "2008-10-02", "2009-01-15", "2010-01-01"],
        )
        self.assertEqual(loaded, expected)

    def test_lookup_includes_a_vintage_published_on_the_as_of_date(self):
        self.store.save_history(history())

        day_before = self.lookup("2008-10-02")
        same_day = self.lookup("2008-10-03")
        day_after = self.lookup("2008-10-04")

        self.assertEqual(day_before.revision, VintageValue("2008-09-05", 137473.0))
        self.assertEqual(same_day.revision, VintageValue("2008-10-03", 137477.0))
        self.assertEqual(day_after.revision, VintageValue("2008-10-03", 137477.0))

    def test_lookup_on_the_first_release_date_returns_the_first_release(self):
        self.store.save_history(history())

        lookup = self.lookup("2008-09-05")

        self.assertTrue(lookup.observation_exists)
        self.assertEqual(lookup.revision, VintageValue("2008-09-05", 137473.0))

    def test_lookup_before_the_first_release_finds_the_observation_only(self):
        self.store.save_history(history())

        lookup = self.lookup("2008-09-04")

        self.assertTrue(lookup.observation_exists)
        self.assertIsNone(lookup.revision)

    def test_lookup_after_the_latest_release_returns_the_latest(self):
        self.store.save_history(history())

        lookup = self.lookup("2026-01-01")

        self.assertTrue(lookup.observation_exists)
        self.assertEqual(lookup.revision, VintageValue("2009-02-06", 137211.0))

    def test_lookup_matches_the_model_on_every_boundary(self):
        expected = history()
        self.store.save_history(expected)

        for as_of_date in (
            "2008-09-04",
            "2008-09-05",
            "2008-09-06",
            "2008-10-02",
            "2008-10-03",
            "2009-02-05",
            "2009-02-06",
            "2009-02-07",
            "9999-12-31",
        ):
            with self.subTest(as_of_date=as_of_date):
                self.assertEqual(
                    self.lookup(as_of_date).revision,
                    expected.value_as_of(as_of_date),
                )

    def test_missing_observation_is_distinct_from_an_unpublished_one(self):
        self.store.save_history(history())

        missing_date = self.lookup("2008-09-12", observation_date="1999-01-01")
        missing_series = self.lookup("2008-09-12", series_id="UNRATE")

        for lookup in (missing_date, missing_series):
            self.assertFalse(lookup.observation_exists)
            self.assertIsNone(lookup.revision)
        self.assertIsNone(self.load(observation_date="1999-01-01"))
        self.assertIsNone(self.load(series_id="UNRATE"))
        self.assertEqual(self.store.list_observations(series_id="UNRATE"), ())

    def test_similar_series_and_dates_do_not_share_data(self):
        stored = {
            ("A", "2008-08-01"): 1.0,
            ("A", "2008-08-10"): 2.0,
            ("A.B", "2008-08-01"): 3.0,
            ("A-B", "2008-08-01"): 4.0,
            ("a", "2008-08-01"): 5.0,
            ("AB", "2008-08-01"): 6.0,
        }
        self.store.save_histories(
            history(
                series_id=series_id,
                observation_date=observation_date,
                revisions=(VintageValue("2008-09-05", value),),
            )
            for (series_id, observation_date), value in stored.items()
        )

        for (series_id, observation_date), value in stored.items():
            with self.subTest(series_id=series_id, observation_date=observation_date):
                lookup = self.lookup(
                    "2008-09-05",
                    series_id=series_id,
                    observation_date=observation_date,
                )
                self.assertEqual(lookup.revision, VintageValue("2008-09-05", value))
        self.assertEqual(
            self.store.list_observations(series_id="A"),
            ("2008-08-01", "2008-08-10"),
        )
        self.assertEqual(self.store.list_observations(series_id="A.B"), ("2008-08-01",))
        self.assertFalse(
            self.lookup("2008-09-05", series_id="A.B", observation_date="2008-08-10")
            .observation_exists
        )
        self.assertEqual(self.store.list_observations(series_id="B"), ())

    def test_values_round_trip_as_exact_floats(self):
        values = (0.1, -2.5, 1e-7, 1e20, 123456.789, 0.0, 4.35e-05, 2.0**53 + 2)
        expected = history(
            revisions=tuple(
                VintageValue(f"2008-09-{day:02d}", value)
                for day, value in enumerate(values, start=1)
            ),
        )

        self.store.save_history(expected)

        loaded = self.load()
        self.assertEqual(loaded, expected)
        for revision in loaded.revisions:
            self.assertIs(type(revision.value), float)
        self.assertIs(type(loaded.total_revision), float)
        self.assertEqual(loaded.total_revision, values[-1] - values[0])
        self.assertIs(type(self.lookup("2008-09-01").revision.value), float)

    def test_store_reports_ready_until_it_is_closed(self):
        self.assertTrue(self.store.is_ready())

        self.store.close()

        self.assertFalse(self.store.is_ready())

    def test_close_can_be_repeated(self):
        self.store.close()
        self.store.close()

    def test_context_manager_returns_the_store_and_closes_it(self):
        with self.store as entered:
            entered.save_history(history())
            self.assertIs(entered, self.store)

        self.assertFalse(self.store.is_ready())

    @patch("asof.sync.get_revision_histories_chunked")
    def test_sync_stores_fetched_histories(self, mock_get_histories):
        mock_get_histories.return_value = (
            history(observation_date="2008-08-01"),
            history(
                observation_date="2008-09-01",
                revisions=(VintageValue("2008-10-03", 136978.0),),
            ),
        )

        result = sync_revision_histories(
            store=self.store,
            series_id="PAYEMS",
            observation_start="2008-08-01",
            observation_end="2008-09-01",
            api_key="test-key",
        )

        self.assertEqual(result.observations, 2)
        self.assertEqual(result.vintages, 4)
        self.assertEqual(
            self.store.list_observations(series_id="PAYEMS"),
            ("2008-08-01", "2008-09-01"),
        )
        self.assertEqual(
            self.lookup("2008-09-12").revision,
            VintageValue("2008-09-05", 137473.0),
        )
        self.assertEqual(
            self.lookup("2008-10-03", observation_date="2008-09-01").revision,
            VintageValue("2008-10-03", 136978.0),
        )


class SQLiteContractTests(RevisionStoreContract, unittest.TestCase):
    def setUp(self):
        self.store = SQLiteRevisionStore(":memory:")
        self.addCleanup(self.store.close)


@unittest.skipUnless(dynamodb_support.AVAILABLE, dynamodb_support.SKIP_REASON)
class DynamoDBContractTests(RevisionStoreContract, unittest.TestCase):
    def setUp(self):
        local = dynamodb_support.LocalDynamoDB().start()
        self.addCleanup(local.stop)
        self.store = DynamoDBRevisionStore(dynamodb_support.TABLE_NAME, client=local.client)
        self.addCleanup(self.store.close)


if __name__ == "__main__":
    unittest.main()
